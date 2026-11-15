#!/usr/bin/env python3
"""Check the status Worker against a real local D1, on demand.

Runs worker/index.js under `wrangler dev` with a throwaway D1, then drives it with
HTTP requests and reads the database behind it, so the CREATE TABLE, the
ON CONFLICT upsert and the response headers are exercised by SQLite and workerd
rather than by the fake in worker/http.test.mjs.

    python tools/check_worker_d1.py            # full run, cleans up after itself
    python tools/check_worker_d1.py --keep     # leave the database to look at

Exit status is 0 only if every check passed. The assertions avoid depending on
whether the XR account is online, because the Worker reads IMVU for real: the
checks look at what the upsert did to the row (the window cleared, one row left),
not at which label the current reading produced.

Needs node/npx; the first run downloads wrangler into the npm cache.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "worker", "wrangler.test.jsonc")
DATABASE = "xr-status-test"

if not shutil.which("npx"):
    raise SystemExit("npx is needed to run wrangler; install Node 18 or newer")

# npx is a .cmd on Windows, which CreateProcess will not start on its own
WRANGLER = (["cmd", "/c", "npx"] if os.name == "nt" else ["npx"]) + ["--yes", "wrangler@4"]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalWorker:
    """`wrangler dev` on a private port, with its state in a temp directory."""

    def __init__(self, state_dir, port):
        self.state_dir = state_dir
        self.port = port
        self.log = os.path.join(state_dir, "wrangler.log")
        self.process = None

    def start(self, timeout=90):
        self.handle = open(self.log, "w", encoding="utf-8", errors="replace")
        self.process = subprocess.Popen(
            WRANGLER
            + [
                "dev",
                "--config",
                CONFIG,
                "--port",
                str(self.port),
                "--local",
                "--persist-to",
                self.state_dir,
            ],
            cwd=ROOT,
            stdout=self.handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise SystemExit("wrangler dev exited:\n" + self.tail())
            try:
                with urllib.request.urlopen(self.url, timeout=2) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(1)
        raise SystemExit("the Worker did not answer within %ss:\n%s" % (timeout, self.tail()))

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.port

    def tail(self, lines=25):
        try:
            with open(self.log, encoding="utf-8", errors="replace") as handle:
                return "".join(handle.readlines()[-lines:])
        except OSError:
            return "(no log)"

    def stop(self):
        if self.process and self.process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    capture_output=True,
                )
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
        try:
            self.handle.close()
        except (AttributeError, OSError):
            pass


def clear(path, attempts=6):
    """Remove a directory, retrying: Windows keeps handles open a moment after exit."""
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(2)
    return False


def request(url, method="GET"):
    """(status, headers, body) for one request; errors are returned, not raised."""
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read().decode("utf-8")


class Checks:
    def __init__(self):
        self.failures = []

    def run(self, name, call):
        try:
            call()
        except AssertionError as error:
            self.failures.append((name, str(error)))
            print("  FAIL  %s" % name)
            print("        %s" % str(error).replace("\n", "\n        "))
        else:
            print("  ok    %s" % name)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the database and log when done")
    args = parser.parse_args()

    state_dir = tempfile.mkdtemp(prefix="xr-d1-check-")
    port = free_port()
    worker = LocalWorker(state_dir, port)

    def sql(statement):
        completed = subprocess.run(
            WRANGLER
            + [
                "d1",
                "execute",
                DATABASE,
                "--local",
                "--config",
                CONFIG,
                "--persist-to",
                state_dir,
                "--json",
                "--command",
                statement,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            raise AssertionError("d1 execute failed: " + (completed.stderr or completed.stdout).strip()[-400:])
        payload = json.loads(completed.stdout[completed.stdout.index("[") :])
        first = payload[0] if isinstance(payload, list) else payload
        return first.get("results") or []

    def rows():
        return sql("SELECT id, label, streak, offline_since, (SELECT count(*) FROM presence) AS n FROM presence")

    def seed(statement):
        sql("UPDATE presence SET " + statement)

    print("worker: %s" % worker.url)
    print("state:  %s" % state_dir)
    checks = Checks()
    try:
        worker.start()
        print("\nHTTP and schema")

        status, headers, body = request(worker.url)

        def answers():
            assert status == 200, "status %s, body %s" % (status, body.strip())
            payload = json.loads(body)
            assert sorted(payload) == ["source", "status", "updated_at"], payload
            assert payload["status"] in ("Online", "Offline"), payload["status"] == "an invented label"
            assert payload["source"] == "public"

        checks.run("the Worker answers on a fresh database", answers)

        def schema():
            found = sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='presence'")
            assert found, "there is no presence table after the first request"
            definition = found[0]["sql"]
            assert "PRIMARY KEY" in definition.upper(), "no primary key, so ON CONFLICT would fail: " + definition

        checks.run("the first request creates the table, with the key ON CONFLICT needs", schema)

        def inserted():
            got = rows()
            assert len(got) == 1, "expected one row, found %d" % len(got)
            assert got[0]["id"] == "xr", got[0]
            assert got[0]["offline_since"] is None, "a cold start has no window open"

        checks.run("the first request inserted exactly one row", inserted)

        def upsert():
            seed("label='Online', streak=0, offline_since=cast(strftime('%s','now') as integer)-400")
            assert rows()[0]["label"] == "Online" and rows()[0]["offline_since"] is not None, "seed failed"
            status2, _, body2 = request(worker.url)
            assert status2 == 200, body2
            after = rows()[0]
            assert after["n"] == 1, "the upsert duplicated the row: %d rows" % after["n"]
            assert after["offline_since"] is None, "the seeded window was not cleared: %s" % after
            assert after["label"] in ("Online", "Offline"), after

        checks.run("a change updates the existing row through ON CONFLICT", upsert)

        def reinsert():
            sql("DELETE FROM presence")
            assert rows() == [], "the delete did not take"
            request(worker.url)
            after = rows()
            assert len(after) == 1, "expected the row back, found %d" % len(after)

        checks.run("a change against a missing row inserts one", reinsert)

        def concurrent():
            sql("DELETE FROM presence")
            results = []

            def hit():
                results.append(request(worker.url)[0])

            threads = [threading.Thread(target=hit) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert results == [200] * 5, "some requests failed: %s" % results
            after = rows()
            assert len(after) == 1, "concurrent requests left %d rows" % len(after)

        checks.run("five concurrent requests leave one row", concurrent)

        def get_headers():
            _, head, _ = request(worker.url)
            cache = (head.get("cache-control") or "").lower()
            assert head.get("access-control-allow-origin") == "*", "no CORS origin"
            assert "no-store" in cache and "max-age=0" in cache, cache
            assert "application/json" in (head.get("content-type") or ""), head.get("content-type")
            assert "x-xr-state" in (head.get("access-control-expose-headers") or ""), "marker not exposed"
            assert head.get("x-xr-state") in ("settled",) or (head.get("x-xr-state") or "").startswith("degraded"), head.get("x-xr-state")

        checks.run("GET carries no-store, CORS and the state marker", get_headers)

        def preflight():
            status3, head, _ = request(worker.url, "OPTIONS")
            assert status3 == 204, status3
            assert "GET" in (head.get("access-control-allow-methods") or ""), head.get("access-control-allow-methods")
            assert "600" in (head.get("access-control-max-age") or ""), "no preflight cache"

        checks.run("OPTIONS is a 204 preflight", preflight)

        def post():
            status4, head, body4 = request(worker.url, "POST")
            assert status4 == 405, status4
            assert "GET" in (head.get("allow") or ""), head.get("allow")
            assert "error" in json.loads(body4), body4

        checks.run("POST is refused with 405 and an Allow header", post)

        def head_only():
            status5, head, body5 = request(worker.url, "HEAD")
            assert status5 == 200, status5
            assert body5 == "", "HEAD returned a body: %r" % body5[:60]
            assert "no-store" in (head.get("cache-control") or "").lower(), head.get("cache-control")

        checks.run("HEAD answers with headers and no body", head_only)

    finally:
        worker.stop()
        if args.keep:
            print("\nkept: %s\nlog:   %s" % (state_dir, worker.log))
        elif not clear(state_dir):
            print("\ncould not remove %s (a handle is still open); delete it yourself" % state_dir)

    if checks.failures:
        print("\n%d check(s) failed" % len(checks.failures))
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
