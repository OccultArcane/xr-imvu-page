/**
 * XR presence endpoint.
 *
 * The GitHub Pages copy of the page is static, so assets/status.json there is a
 * frozen commit. This answers the same question live: is the XR account present
 * on IMVU right now? It reads the same public record tools/track-status.py reads
 * (api.imvu.com/user/user-106440837), checks the username really is "xr" before
 * reporting anything, and answers with the three keys the plate already knows:
 *
 *   { "status": "Online" | "Offline", "source": "public", "updated_at": "<ISO>" }
 *
 * Raw readings are not safe to publish. Measured over a minute, IMVU's flag
 * flips True/False on nearly every 30s sample while the client is open, which is
 * why the watcher on the desktop confirms online twice and waits six minutes
 * before believing an offline reading. The same rules run here against a single
 * row in D1, so every visitor gets the settled label rather than a different one
 * each poll:
 *
 *   ONLINE_CONFIRM  two agreeing samples before claiming Online
 *   ONLINE_GRACE    six minutes of offline before believing it
 *
 * No-store out, no-store in, permissive CORS for the Pages origin. A failed read
 * answers 502 with an error object rather than "Offline": answering offline for
 * an account nobody could check is the one wrong answer this endpoint must never
 * give. If the database is unavailable the reading is reported raw, with
 * `x-xr-state: degraded` on the response so a flapping label can be explained.
 */

const USER_URL = "https://api.imvu.com/user/user-106440837";
const EXPECTED_USERNAME = "xr";
const UPSTREAM_TIMEOUT_MS = 8000;
const ONLINE_CONFIRM = 2;
const ONLINE_GRACE = 6 * 60;
const ROW_ID = "xr";

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, HEAD, OPTIONS",
  "access-control-allow-headers": "accept, content-type",
  "access-control-expose-headers": "x-xr-state",
  "access-control-max-age": "600",
};

let schemaReady = false;

function reply(body, status, extra = {}, method = "GET") {
  return new Response(method === "HEAD" ? null : JSON.stringify(body) + "\n", {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store, no-cache, must-revalidate, max-age=0",
      pragma: "no-cache",
      ...CORS,
      ...extra,
    },
  });
}

function stamp() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

async function readPresence() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  let payload;
  try {
    const response = await fetch(USER_URL, {
      headers: {
        accept: "application/json",
        "user-agent": "xr-status/1.0 (+https://occultarcane.github.io/xr-imvu-page/)",
      },
      signal: controller.signal,
      // never let Cloudflare answer this from its own edge cache
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    if (!response.ok) throw new Error("IMVU answered " + response.status);
    payload = await response.json();
  } finally {
    clearTimeout(timer);
  }

  const denormalized = (payload && payload.denormalized) || {};
  const entry = denormalized[payload && payload.id] || Object.values(denormalized)[0] || {};
  const data = entry.data || {};

  if (!("online" in data)) throw new Error("no presence flag in IMVU's response");

  // a wrong id must fail loudly, exactly as the watcher insists
  const username = String(data.username || "").trim();
  if (username.toLowerCase() !== EXPECTED_USERNAME) {
    throw new Error('member is "' + (username || "(none)") + '", not "' + EXPECTED_USERNAME + '"');
  }

  return Boolean(data.online);
}

async function ensureSchema(db) {
  if (schemaReady) return;
  await db
    .prepare(
      "CREATE TABLE IF NOT EXISTS presence (" +
        "id TEXT PRIMARY KEY, label TEXT NOT NULL, streak INTEGER NOT NULL, " +
        "offline_since INTEGER)"
    )
    .run();
  schemaReady = true;
}

/**
 * The watcher's rules, verbatim: confirm an online reading twice, and let an
 * offline one stand for six minutes before it is believed. Returns the label to
 * publish and whether the stored row had to change.
 */
function settle(previous, online, nowSeconds) {
  let label = previous ? previous.label : online ? "Online" : "Offline";
  let streak = previous ? previous.streak : 0;
  let offlineSince = previous ? previous.offline_since : null;

  if (online) {
    offlineSince = null;
    if (label !== "Online") {
      streak += 1;
      if (streak >= ONLINE_CONFIRM) {
        label = "Online";
        streak = 0;
      }
    } else {
      streak = 0;
    }
  } else if (label === "Online") {
    if (offlineSince === null) offlineSince = nowSeconds;
    if (nowSeconds - offlineSince >= ONLINE_GRACE) {
      label = "Offline";
      offlineSince = null;
      streak = 0;
    }
  } else {
    streak = 0;
  }

  const changed =
    !previous ||
    previous.label !== label ||
    previous.streak !== streak ||
    (previous.offline_since === null) !== (offlineSince === null) ||
    (offlineSince !== null && previous.offline_since !== offlineSince);

  return { label, streak, offlineSince, changed };
}

// the settling rule is exported so it can be tested without IMVU or a deployment
// (worker/settle.test.mjs); nothing else imports it
export { settle, ONLINE_CONFIRM, ONLINE_GRACE };

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return reply({ error: "method not allowed" }, 405, { allow: "GET, HEAD, OPTIONS" }, request.method);
    }

    let online;
    try {
      online = await readPresence();
    } catch (error) {
      return reply({ error: String((error && error.message) || error) }, 502, {}, request.method);
    }

    const nowSeconds = Math.floor(Date.now() / 1000);
    const db = env && env.DB;
    if (!db) {
      return reply(
        { status: online ? "Online" : "Offline", source: "public", updated_at: stamp() },
        200,
        { "x-xr-state": "degraded" },
        request.method
      );
    }

    try {
      await ensureSchema(db);
      const previous = await db
        .prepare("SELECT label, streak, offline_since FROM presence WHERE id = ?")
        .bind(ROW_ID)
        .first();
      const result = settle(previous, online, nowSeconds);

      if (result.changed) {
        await db
          .prepare(
            "INSERT INTO presence (id, label, streak, offline_since) VALUES (?, ?, ?, ?) " +
              "ON CONFLICT(id) DO UPDATE SET label = excluded.label, " +
              "streak = excluded.streak, offline_since = excluded.offline_since"
          )
          .bind(ROW_ID, result.label, result.streak, result.offlineSince)
          .run();
      }

      return reply(
        { status: result.label, source: "public", updated_at: stamp() },
        200,
        { "x-xr-state": "settled" },
        request.method
      );
    } catch (error) {
      // the read worked; do not fail the visitor over the smoothing state
      return reply(
        { status: online ? "Online" : "Offline", source: "public", updated_at: stamp() },
        200,
        { "x-xr-state": "degraded: " + String((error && error.message) || error).slice(0, 80) },
        request.method
      );
    }
  },
};
