#!/usr/bin/env python3
"""Tests for tools/track-status.py — the presence watcher.

Everything runs in a temp directory and replaces `read_presence` with a stub, so
no test touches IMVU. The rate-limit cases at the bottom are the exception: they
point the module at a loopback server that answers the way IMVU does when it is
asked too often, so that path is exercised for real over a socket.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TRACK = os.path.join(HERE, "track-status.py")
SERVE = os.path.join(HERE, "serve.py")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Harness:
    """A watcher wired to a temp directory and a stubbed presence read."""

    def __init__(self, stub=True):
        root = tempfile.mkdtemp()
        track = load(TRACK, "track_status_under_test")

        track.JSON_PATH = os.path.join(root, "status.json")
        track.OVERRIDE_PATH = os.path.join(root, "status.override.txt")
        track.NOTIFY_PATH = os.path.join(root, "notify.url")
        track.LOG_PATH = os.path.join(root, "status.log")
        track.STATE_PATH = os.path.join(root, "state.json")
        track.SAMPLE_LOG = os.path.join(root, "availability.log")
        track._throttled_until = 0.0

        if stub:
            track.read_presence = lambda member, expect: False

        self.track = track
        self.root = root
        self.args = argparse.Namespace(
            member=track.XR_MEMBER, expect=track.XR_NAME, dry_run=False, watch=0
        )

    # --- helpers -----------------------------------------------------------

    def poll(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.track.poll(self.args)

    def status(self):
        try:
            with open(self.track.JSON_PATH, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def write_status(self, payload):
        with open(self.track.JSON_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def state(self):
        try:
            with open(self.track.STATE_PATH, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def write_state(self, state):
        with open(self.track.STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

    def notes(self):
        """Log lines with the timestamp stripped."""
        try:
            with open(self.track.LOG_PATH, encoding="utf-8") as handle:
                lines = [line.rstrip("\n") for line in handle if line.strip()]
        except OSError:
            return []
        return [re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", "", line) for line in lines]

    def samples(self):
        try:
            with open(self.track.SAMPLE_LOG, encoding="utf-8") as handle:
                return [line.rstrip("\n") for line in handle if line.strip()]
        except OSError:
            return []

    def override(self, text):
        with open(self.track.OVERRIDE_PATH, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")


class PresenceRules(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.track = self.harness.track

    # --- the label ---------------------------------------------------------

    def test_an_offline_first_sample_is_offline(self):
        self.harness.poll()
        status = self.harness.status()
        self.assertEqual(status["status"], "Offline")
        self.assertEqual(status["source"], "public")
        self.assertRegex(status["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # the session machinery is gone: only a label, where it came from, and when
        self.assertEqual(set(status), {"status", "source", "updated_at"})

    def test_the_first_ever_sample_seeds_the_label(self):
        # nothing published and no state to hold: the reading decides, the same as
        # the first request into an empty D1 row
        self.track.read_presence = lambda member, expect: True
        self.harness.poll()
        self.assertEqual(self.harness.status()["status"], "Online")

    def test_online_needs_two_agreeing_samples(self):
        # a label is already out there, so one true sample cannot move it
        self.harness.write_status(
            {"status": "Offline", "source": "public", "updated_at": "2026-01-01T00:00:00Z"}
        )
        self.track.read_presence = lambda member, expect: True
        self.harness.poll()
        self.assertEqual(self.harness.status()["status"], "Offline", "one sample is not a session")
        self.harness.poll()
        self.assertEqual(self.harness.status()["status"], "Online")

    def test_a_short_gap_holds_the_label(self):
        self.track.read_presence = lambda member, expect: True
        self.harness.poll()
        self.harness.poll()
        self.assertEqual(self.harness.status()["status"], "Online")

        # the heartbeat drops for one sample: still online, the client is open
        self.track.read_presence = lambda member, expect: False
        self.harness.poll()
        self.assertEqual(self.harness.status()["status"], "Online")

        # once the window outlasts the grace, it is believed
        state = self.harness.state()
        self.assertIsNotNone(state.get("offline_since"), "the window opens on the first offline sample")
        state["offline_since"] = time.time() - self.track.ONLINE_GRACE - 1
        self.harness.write_state(state)
        self.harness.poll()
        self.assertEqual(self.harness.status()["status"], "Offline")

    def test_a_stray_true_does_not_flash_offline(self):
        # the measured flapping: a true after a gap is not yet a session, and it
        # must not claim Offline on the way (the Worker holds the label here too)
        self.track.read_presence = lambda member, expect: True
        self.harness.poll()
        self.harness.poll()
        self.assertEqual(self.harness.status()["status"], "Online")

        self.track.read_presence = lambda member, expect: False
        self.harness.poll()
        self.track.read_presence = lambda member, expect: True
        self.harness.poll()

        self.assertEqual(self.harness.status()["status"], "Online")

    def test_a_change_is_logged_once(self):
        self.harness.poll()  # nothing published, reading false: Offline
        self.track.read_presence = lambda member, expect: True
        self.harness.poll()  # one true sample: held, still Offline
        self.harness.poll()  # confirmed: Online
        self.harness.poll()  # unchanged: nothing new to say
        self.assertEqual(self.harness.notes(), ["status: Offline", "status: Online"])

    def test_a_failed_read_keeps_the_label(self):
        self.track.read_presence = lambda member, expect: True
        self.harness.poll()
        self.harness.poll()
        before = self.harness.status()

        def broken(member, expect):
            raise LookupError("member 1 is 'nobody', not 'xr'")

        self.track.read_presence = broken
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.harness.poll(), 1)
        self.assertEqual(self.harness.status(), before)
        self.assertTrue(any("failed:" in line for line in self.harness.notes()))

    def test_the_sample_line_says_what_was_read(self):
        self.harness.poll()
        sample = self.harness.samples()[-1]
        self.assertIn("source=public", sample)
        self.assertIn("online=False", sample)
        self.assertIn("label=Offline", sample)
        self.assertNotIn("session=", sample)

    # --- freshness ---------------------------------------------------------

    def test_every_poll_writes_a_new_timestamp(self):
        self.harness.poll()
        first = self.harness.status()["updated_at"]

        self.track.now_stamp = lambda: "2026-01-01T00:00:05Z"
        self.harness.poll()
        self.assertEqual(self.harness.status()["updated_at"], "2026-01-01T00:00:05Z")
        self.assertNotEqual(self.harness.status()["updated_at"], first)
        self.assertEqual(self.harness.status()["status"], "Offline")

    def test_a_pause_refreshes_the_stamp_without_touching_the_label(self):
        self.harness.poll()
        before = self.harness.status()["status"]

        self.track.now_stamp = lambda: "2026-01-01T00:00:09Z"
        self.track._throttled_until = time.time() + 60
        self.harness.poll()

        status = self.harness.status()
        self.assertEqual(status["status"], before)
        self.assertEqual(status["updated_at"], "2026-01-01T00:00:09Z")

    def test_a_dry_run_writes_nothing(self):
        self.harness.args.dry_run = True
        self.harness.poll()
        self.assertEqual(self.harness.status(), {})

    # --- labels set by hand ------------------------------------------------

    def test_a_manual_label_is_held(self):
        self.track.read_presence = lambda member, expect: True
        self.harness.poll()
        self.harness.poll()
        current = self.harness.status()
        current["manual"] = True
        self.harness.write_status(current)
        samples_before = len(self.harness.samples())

        self.harness.poll()
        self.assertEqual(self.harness.status()["status"], "Online", "held, not re-read")
        self.assertEqual(
            len(self.harness.samples()), samples_before, "no IMVU sample while held"
        )

    def test_the_override_file_wins(self):
        self.harness.override("Creating")
        self.harness.poll()
        status = self.harness.status()
        self.assertEqual(status["status"], "Creating")
        self.assertEqual(status["source"], "override")
        self.assertIn("source=override", self.harness.samples()[-1])


class RealRateLimit(unittest.TestCase):
    """A 429, or a body that says so, must pause the watcher rather than be believed."""

    def serve(self, status, body):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/user" % server.server_address[1]

    def test_a_429_pauses_and_keeps_the_label(self):
        harness = Harness(stub=False)
        harness.track.USER_URL = self.serve(429, "slow down")
        harness.poll()

        self.assertEqual(harness.status(), {})
        self.assertGreater(harness.track.throttle_left(), 0)
        self.assertTrue(any("throttled:" in line for line in harness.notes()))

    def test_a_body_saying_too_many_requests_pauses_too(self):
        harness = Harness(stub=False)
        harness.track.USER_URL = self.serve(200, "You have made too many requests recently")
        harness.poll()

        self.assertEqual(harness.status(), {})
        self.assertGreater(harness.track.throttle_left(), 0)


class Notifications(unittest.TestCase):
    """A label change goes to the webhook once, and a broken webhook is survivable."""

    def serve(self, status=204):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen.append(
                    {
                        "path": self.path,
                        "type": self.headers.get("Content-Type"),
                        "body": self.rfile.read(length).decode("utf-8"),
                    }
                )
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/hook" % server.server_address[1], seen

    def listen(self, harness, url):
        with open(harness.track.NOTIFY_PATH, "w", encoding="utf-8") as handle:
            handle.write(url + "\n")

    def test_no_webhook_means_no_request(self):
        harness = Harness()
        harness.poll()
        self.assertEqual(harness.status()["status"], "Offline")
        self.assertFalse(any("notified" in line or "notify failed" in line for line in harness.notes()))

    def test_a_change_is_posted_once(self):
        harness = Harness()
        url, seen = self.serve()
        self.listen(harness, url)

        harness.poll()  # nothing published yet: Offline is a change
        harness.poll()  # unchanged: no second post

        self.assertEqual(len(seen), 1)
        self.assertIn("Offline", seen[0]["body"])
        self.assertTrue(any("notified:" in line for line in harness.notes()))

    def test_a_discord_webhook_gets_json(self):
        harness = Harness()
        url, seen = self.serve()
        self.listen(harness, url.replace("/hook", "/discord.com/api/webhooks/x"))

        harness.poll()

        self.assertEqual(len(seen), 1)
        self.assertIn("application/json", seen[0]["type"])
        self.assertIn("Offline", json.loads(seen[0]["body"])["content"])

    def test_a_plain_target_gets_text(self):
        harness = Harness()
        url, seen = self.serve()
        self.listen(harness, url)

        harness.poll()

        self.assertIn("text/plain", seen[0]["type"])
        self.assertTrue(seen[0]["body"].startswith("XR is now "))

    def test_a_dead_webhook_does_not_break_the_poll(self):
        harness = Harness()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.listen(harness, "http://127.0.0.1:%d/hook" % port)  # nothing listening

        self.assertEqual(harness.poll(), 0)
        self.assertEqual(harness.status()["status"], "Offline", "the page still gets its label")
        self.assertTrue(any("notify failed:" in line for line in harness.notes()))


class ServeDenyList(unittest.TestCase):
    """The preview server must refuse a stray cookie file however it is spelled."""

    def setUp(self):
        self.serve = load(SERVE, "serve_under_test")

    def refused(self, path):
        handler = self.serve.Handler.__new__(self.serve.Handler)
        handler.path = path
        return handler._refused()

    def test_cookie_files_are_refused_in_every_encoding(self):
        for path in (
            "/assets/session.cookies",
            "/assets/session%2Ecookies",
            "/assets/session%2ecookies",
            "/assets/%2Esession.cookies",
            "/assets/session%252Ecookies",
            "/session.cookies?v=1",
        ):
            self.assertTrue(self.refused(path), path)

    def test_ordinary_files_are_still_served(self):
        for path in (
            "/index.html",
            "/styles.css",
            "/assets/status.json",
            "/assets/products.json",
            "/assets/notification.txt",
        ):
            self.assertFalse(self.refused(path), path)

    def test_dotfiles_are_refused(self):
        for path in ("/assets/.env", "/.git/config", "/assets/%2Eenv"):
            self.assertTrue(self.refused(path), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
