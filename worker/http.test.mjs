/**
 * HTTP-layer tests for the presence endpoint.
 *
 * worker/settle.test.mjs covers the rule; this covers the handler around it:
 * what a visitor gets when IMVU cannot be reached, when D1 misbehaves, and what
 * headers every answer carries. No deployment and no network — the upstream
 * `fetch` is stubbed and `env.DB` is a fake that can be told to fail.
 *
 *   node --test worker/
 */

import test from "node:test";
import assert from "node:assert/strict";

import worker from "./index.js";

const USER_URL = "https://api.imvu.com/user/user-106440837";
const ENDPOINT = "https://status.test/";

/**
 * IMVU's real payload shape, trimmed to what the endpoint reads: a denormalized
 * map keyed by the node URL, with `data.username` and `data.online` inside.
 */
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
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init: init || {} });
    return handler(String(url), init || {});
  };
  return {
    calls,
    restore() {
      globalThis.fetch = original;
    },
  };
}

/**
 * A fake env.DB. `failOn` names the operation to break: "prepare", "first" or
 * "run". Writes are recorded so a test can assert nothing was written.
 */
function fakeEnv({ row = null, failOn = null } = {}) {
  const writes = [];
  const db = {
    prepare(sql) {
      if (failOn === "prepare") throw new Error("D1 unavailable");
      return {
        sql,
        args: [],
        bind(...args) {
          this.args = args;
          return this;
        },
        async first() {
          if (failOn === "first") throw new Error("D1 read failed");
          return row;
        },
        async run() {
          if (failOn === "run") throw new Error("D1 write failed");
          writes.push({ sql, args: this.args });
          return { success: true };
        },
      };
    },
  };
  return { env: { DB: db }, writes };
}

async function call(env, init = {}) {
  return worker.fetch(new Request(ENDPOINT, init), env);
}

async function body(response) {
  return JSON.parse(await response.text());
}

function assertCors(response, label = "") {
  assert.equal(response.headers.get("access-control-allow-origin"), "*", "CORS origin " + label);
  assert.match(response.headers.get("content-type") || "", /application\/json/, "json content type " + label);
}

function assertNoStore(response, label = "") {
  const cache = (response.headers.get("cache-control") || "").toLowerCase();
  assert.match(cache, /no-store/, "no-store missing " + label);
  assert.match(cache, /max-age=0/, "max-age=0 missing " + label);
}

test("a good reading answers the three keys with no-store and CORS", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());
  const { env } = fakeEnv({ row: null });

  const response = await call(env);

  assert.equal(response.status, 200);
  assertCors(response, "on success");
  assertNoStore(response, "on success");
  assert.equal(response.headers.get("x-xr-state"), "settled");

  const payload = await body(response);
  assert.deepEqual(Object.keys(payload).sort(), ["source", "status", "updated_at"]);
  assert.equal(payload.status, "Online");
  assert.equal(payload.source, "public");
  assert.match(payload.updated_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);

  assert.equal(fetchStub.calls.length, 1);
  assert.equal(fetchStub.calls[0].url, USER_URL);
  // the upstream read must not be answered from Cloudflare's edge cache
  assert.equal(fetchStub.calls[0].init.cf.cacheTtl, 0, "upstream cache disabled");
});

test("the stored row decides the label, not the raw reading", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());
  // offline until two samples agree: one online sample only moves the streak
  const { env, writes } = fakeEnv({ row: { label: "Offline", streak: 0, offline_since: null } });

  const payload = await body(await call(env));

  assert.equal(payload.status, "Offline");
  const labelWrite = writes.find((w) => /INSERT INTO presence/.test(w.sql));
  assert.ok(labelWrite, "the streak change is persisted");
  assert.deepEqual(labelWrite.args, ["xr", "Offline", 1, null]);
});

test("a steady reading writes no row", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());
  const { env, writes } = fakeEnv({ row: { label: "Online", streak: 0, offline_since: null } });

  const payload = await body(await call(env));

  assert.equal(payload.status, "Online");
  assert.equal(writes.filter((w) => /INSERT INTO presence/.test(w.sql)).length, 0);
});

test("IMVU being unreachable is a 502, never an offline reading", async (t) => {
  const fetchStub = stubFetch(async () => {
    throw new Error("getaddrinfo ENOTFOUND api.imvu.com");
  });
  t.after(() => fetchStub.restore());
  const { env, writes } = fakeEnv({ row: null });

  const response = await call(env);

  assert.equal(response.status, 502);
  assertCors(response, "on 502");
  assertNoStore(response, "on 502");
  const payload = await body(response);
  assert.match(payload.error, /ENOTFOUND/);
  assert.equal(payload.status, undefined, "no status is invented");
  assert.equal(writes.length, 0, "nothing is written when the read failed");
});

test("an HTTP error from IMVU is a 502", async (t) => {
  const fetchStub = stubFetch(async () => new Response("nope", { status: 503 }));
  t.after(() => fetchStub.restore());

  const response = await call(fakeEnv().env);

  assert.equal(response.status, 502);
  assert.match((await body(response)).error, /503/);
});

test("a payload without a presence flag is a 502", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse({ id: USER_URL, denormalized: { [USER_URL]: { data: {} } } }));
  t.after(() => fetchStub.restore());

  const response = await call(fakeEnv().env);

  assert.equal(response.status, 502);
  assert.match((await body(response)).error, /presence flag/);
});

test("a payload for the wrong account is a 502", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true, "someone-else")));
  t.after(() => fetchStub.restore());

  const response = await call(fakeEnv().env);

  assert.equal(response.status, 502);
  const { error } = await body(response);
  assert.match(error, /someone-else/);
  assert.match(error, /not "xr"/);
});

test("D1 failing to prepare degrades to the raw reading", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(false)));
  t.after(() => fetchStub.restore());
  const { env } = fakeEnv({ failOn: "prepare" });

  const response = await call(env);

  assert.equal(response.status, 200, "a visitor still gets an answer");
  assertCors(response, "on degraded");
  assertNoStore(response, "on degraded");
  assert.match(response.headers.get("x-xr-state") || "", /^degraded/);
  assert.equal((await body(response)).status, "Offline", "the raw reading, unsettled");
});

test("D1 failing to read degrades to the raw reading", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());
  const { env } = fakeEnv({ failOn: "first" });

  const response = await call(env);

  assert.equal(response.status, 200);
  assert.match(response.headers.get("x-xr-state") || "", /^degraded: D1 read failed/);
  assert.equal((await body(response)).status, "Online");
});

test("D1 failing to write degrades rather than failing the visitor", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());
  // an empty row is a change, so this path attempts the write and hits the error
  const { env } = fakeEnv({ row: null, failOn: "run" });

  const response = await call(env);

  assert.equal(response.status, 200);
  assert.match(response.headers.get("x-xr-state") || "", /^degraded: D1 write failed/);
});

test("no DB binding at all degrades to the raw reading", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());

  const response = await call({});

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-xr-state"), "degraded");
  assert.equal((await body(response)).status, "Online");
});

test("CORS preflight is answered without touching IMVU", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());

  const response = await call(fakeEnv().env, { method: "OPTIONS" });

  assert.equal(response.status, 204);
  assert.equal(response.headers.get("access-control-allow-origin"), "*");
  assert.match(response.headers.get("access-control-allow-methods") || "", /GET/);
  assert.match(response.headers.get("access-control-allow-headers") || "", /accept/);
  assert.equal(fetchStub.calls.length, 0, "a preflight costs no upstream read");
});

test("the degraded marker is exposed to the browser", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());

  const response = await call(fakeEnv().env);

  assert.match(response.headers.get("access-control-expose-headers") || "", /x-xr-state/);
});

test("a write method is refused with 405 and a hint", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());

  const response = await call(fakeEnv().env, { method: "POST" });

  assert.equal(response.status, 405);
  assert.match(response.headers.get("allow") || "", /GET/);
  assertCors(response, "on 405");
  assert.equal(fetchStub.calls.length, 0);
});

test("HEAD answers with no body", async (t) => {
  const fetchStub = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => fetchStub.restore());

  const response = await call(fakeEnv().env, { method: "HEAD" });

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
  assertNoStore(response, "on HEAD");
});
