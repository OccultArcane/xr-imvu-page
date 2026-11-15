#!/usr/bin/env python3
"""Keep assets/status.json honest by reading IMVU's own presence.

The page shows a dot and a word for the creator's status. That word used to be
whatever someone typed into assets/status.json, so it only changed when a human
remembered to change it. IMVU publishes presence for any member without a login,
so this script asks instead:

    https://api.imvu.com/user/user-<member>   ->  {"online": true, "username": "xr"}

The user record is used rather than the presence resource because presence is
too forgiving: asked about a member id that does not exist, it answers
`{"online": false}` with a 200, so a typo would quietly become "Offline". The
user record 404s instead, and carries the username, which is checked against the
expected one before either answer is believed.

The label is therefore Online or Offline, nothing else. The client's own modes
("Creating", "Do Not Disturb") live behind a signed-in endpoint
(avatars.imvu.com/api/avatarcard.php) that answers an anonymous request with
"this service requires an authenticated request"; the anonymous record leaves
`availability` empty whatever mode the client is in. Reading those modes would
cost a session cookie and a second request per poll, and a session has never
been available here, so this script does not pretend to read them.
assets/status.override.txt still outranks IMVU for a hand-set label — clear the
file to hand control back.

The published flag is also a heartbeat rather than a session. Measured against
the real account it flipped to `online: false` for minutes at a time while the
client was open, which made the dot flash online/offline every few minutes. So
"online" is taken at its word and refreshes a timestamp, while "offline" is only
believed once it has outlasted ONLINE_GRACE — the gap between heartbeats.

    python tools/track-status.py                # the XR member
    python tools/track-status.py --member 123   # someone else
    python tools/track-status.py --dry-run      # say what it would write
    python tools/track-status.py --watch 30     # keep polling, twice a minute

The scheduled task runs `--watch 30`, because Windows Task Scheduler will not
trigger more than once a minute and the page wants an answer sooner than that.
The task's own trigger stays as the restart net: while the watcher is alive
Windows sees the task as running and starts nothing extra.

Without --watch, exit code is 0 when the file is current (written or already
right), 1 when presence could not be read — in which case the last label stays.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(ROOT, "assets", "status.json")

# the member behind the XR shop (manufacturer id and member id are the same number)
XR_MEMBER = "106440837"
XR_NAME = "xr"
USER_URL = "https://api.imvu.com/user/user-{member}"

LOG_PATH = os.path.join(tempfile.gettempdir(), "xr-status.log")
STATE_PATH = os.path.join(tempfile.gettempdir(), "xr-status-state.json")

LOG_MAX = 256 * 1024

# every poll, not just changes: the samples that say nothing happened are the
# evidence that a reading is steady rather than stuck. Twice a minute is about
# 100 KB a day, so it rotates.
SAMPLE_LOG = os.path.join(tempfile.gettempdir(), "xr-availability.log")
SAMPLE_LOG_MAX = 256 * 1024

# a hand-written label that outranks IMVU, e.g. Creating
OVERRIDE_PATH = os.path.join(ROOT, "assets", "status.override.txt")

# how long "offline" has to persist before the dot is allowed to go dark;
# comfortably longer than the observed heartbeat gap of about three minutes
ONLINE_GRACE = 360

# and how many agreeing samples it takes to claim "online": one stray true
# between gaps is not a session
ONLINE_CONFIRM = 2

# IMVU answering 429 is not an error to retry at once: stop asking for a while
THROTTLE_SECONDS = 120
_throttled_until = 0.0

# the tests, run once before the watcher starts watching: a broken edit should
# stop the scheduled task rather than quietly go live there
SELF_CHECK = os.path.join(ROOT, "tools", "test_track_status.py")
SELF_CHECK_TIMEOUT = 120

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
    ),
    "Referer": "https://www.imvu.com/",
    "Accept": "application/json",
}


def note(message):
    """One line of history per change: under pythonw there is no console."""
    try:
        if os.path.getsize(LOG_PATH) >= LOG_MAX:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def log_sample(source, online, label, extra=""):
    """One line per look at IMVU, whatever it said."""
    try:
        if os.path.getsize(SAMPLE_LOG) >= SAMPLE_LOG_MAX:
            os.replace(SAMPLE_LOG, SAMPLE_LOG + ".1")
    except OSError:
        pass
    try:
        with open(SAMPLE_LOG, "a", encoding="utf-8") as handle:
            handle.write(
                "%s source=%-7s online=%-5s label=%s%s\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), source, online, label, extra)
            )
    except OSError:
        pass


def run_self_check():
    """(ok, one-line summary) from running the tests.

    A subprocess rather than an import: the tests load this file by path and
    patch its globals, so running them in-process would mean two copies of the
    module and a self-check that could pass while this copy is wrong.
    """
    if not os.path.exists(SELF_CHECK):
        return True, "no test file"
    try:
        result = subprocess.run(
            [sys.executable, SELF_CHECK],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=SELF_CHECK_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, "could not run: %s" % error

    if result.returncode == 0:
        return True, "passed"
    output = (result.stderr or "") + (result.stdout or "")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    names = []
    for line in lines:
        if not (line.startswith("FAIL:") or line.startswith("ERROR:")):
            continue
        name = line.split(":", 1)[1].strip().split(" (")[0]
        if name and name not in names:
            names.append(name)
    if names:
        shown = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
        return False, "%s failing (%s)" % (len(names), shown)
    return False, (lines[-1] if lines else "exit %s" % result.returncode)


class Throttled(Exception):
    """IMVU asked for fewer requests; nothing is read until the pause ends."""


def throttle(seconds=THROTTLE_SECONDS):
    """Stop asking IMVU for a while: retrying a rate limit is what turns it into a block."""
    global _throttled_until
    _throttled_until = time.time() + seconds


def throttle_left():
    return max(0.0, _throttled_until - time.time())


def request_body(url, headers=None):
    """The body of one request, refusing to run while IMVU is pausing us."""
    left = throttle_left()
    if left > 0:
        raise Throttled("paused for another %ds" % int(left))

    request = urllib.request.Request(url, headers=headers or HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        if error.code == 429:
            throttle()
            raise Throttled("IMVU answered 429; pausing %ds" % THROTTLE_SECONDS) from error
        raise
    if "too many requests" in body.lower():
        throttle()
        raise Throttled("IMVU asked for fewer requests; pausing %ds" % THROTTLE_SECONDS)
    return body


def fetch_json(url):
    return json.loads(request_body(url))


def node_entry(payload):
    """The whole denormalized node — data *and* relations — for the URL asked."""
    denormalized = payload.get("denormalized") or {}
    entry = denormalized.get(payload.get("id"))
    if entry is None and denormalized:
        entry = next(iter(denormalized.values()))
    return entry or {}


def read_presence(member, expect):
    """True when the member is online; raises when the read fails.

    The username is checked here rather than returned: the only thing a caller
    could do with it is what this function already does.
    """
    entry = node_entry(fetch_json(USER_URL.format(member=member)))
    data = entry.get("data") or {}
    if "online" not in data:
        raise LookupError("no presence flag in the response")

    # a wrong id must fail loudly: answering "offline" about a stranger is the
    # one wrong answer this file can give without anyone noticing
    username = (data.get("username") or "").strip()
    if expect and username.lower() != expect.lower():
        raise LookupError(
            "member %s is %r, not %r — refusing to report on the wrong account"
            % (member, username or "(no username)", expect)
        )
    return bool(data["online"])


def read_current():
    try:
        with open(JSON_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_status(payload):
    """Swap the file in whole, so the page never reads half a document."""
    temporary = JSON_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, JSON_PATH)


def read_override():
    """A hand-written label that outranks IMVU, e.g. Creating."""
    try:
        with open(OVERRIDE_PATH, encoding="utf-8") as handle:
            for line in handle:
                label = line.strip()
                if label and not label.startswith("#"):
                    return label
    except OSError:
        pass
    return ""


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        temporary = STATE_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, STATE_PATH)
    except OSError:
        pass


def now_stamp():
    """UTC, second resolution, sortable by eye and by Date.parse."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def status_payload(label, source):
    """What status.json says: the label, and where it came from."""
    return {
        "status": label,
        "source": source,
        "updated_at": now_stamp(),
    }


def payload_matches(current, payload):
    """True when the file already says exactly this.

    `updated_at` is excluded because it is deliberately new on every poll: the
    page needs it even when the label has not moved.
    """
    if set(current) - set(payload) - {"manual", "updated_at"}:
        return False
    return all(
        current.get(key) == value
        for key, value in payload.items()
        if key != "updated_at"
    )


def settle(current, label, args, reason="", source="public"):
    """Write the label and a fresh timestamp on every poll.

    The file is rewritten even when nothing moved, because `updated_at` is the
    only thing that lets the page tell a quiet watcher from a dead one.
    """
    payload = status_payload(label, source)
    same = payload_matches(current, payload)

    if args.dry_run:
        print("would write %s (was %r, source %r)" % (label, current.get("status"), current.get("source")))
        return 0

    write_status(payload)
    if same:
        print("already %s (stamp refreshed)" % label)
    else:
        print("wrote %s (was %r, source %r)" % (label, current.get("status"), current.get("source")))
    if current.get("status") != label:
        note("status: %s%s" % (label, " (%s)" % reason if reason else ""))
    return 0


def touch(current):
    """Refresh the stamp without touching the reading, for a deliberate pause."""
    if not current.get("status"):
        return
    payload = dict(current)
    payload["updated_at"] = now_stamp()
    write_status(payload)


def poll(args):
    """One look at IMVU; a throttle ends the look without touching the label."""
    try:
        return _poll(args)
    except Throttled as error:
        note("throttled: %s — last label kept" % error)
        print("throttled: %s" % error)
        return 0


def settle_label(state, online, now, held=None):
    """The confirm and grace rules, as a label decision on its own.

    This is the Worker's rule (worker/index.js, settle()) and the two are compared
    by tools/test_settle_parity.py, so keep them identical:

      * `held` is the label already published. An online sample that is not yet
        confirmed leaves it alone rather than claiming Offline for a poll — the
        flash the grace exists to prevent. With nothing published yet the reading
        decides, which is what a fresh row in the Worker does.
      * the window starts at the first offline sample, not at the last one seen
        online, so "six minutes offline" means what it says.

    State carries `online_streak` and `offline_since`.
    """
    label = held if held in ("Online", "Offline") else ("Online" if online else "Offline")
    streak = int(state.get("online_streak") or 0)
    offline_since = state.get("offline_since")

    if online:
        offline_since = None
        if label != "Online":
            streak += 1
            if streak >= ONLINE_CONFIRM:
                label = "Online"
                streak = 0
        else:
            streak = 0
    elif label == "Online":
        if offline_since is None:
            offline_since = now
        if now - float(offline_since) >= ONLINE_GRACE:
            label = "Offline"
            offline_since = None
            streak = 0
    else:
        streak = 0

    state["online_streak"] = streak
    state["offline_since"] = offline_since
    return label


def _poll(args):
    """One look at IMVU; writes the label and a fresh timestamp."""
    current = read_current()

    override = read_override()
    if override:
        log_sample("override", "-", override)
        return settle(current, override, args, reason="override file", source="override")

    if current.get("manual"):
        print("manual status held: %s (remove \"manual\" to let this run again)" % current.get("status"))
        # pinned by hand, but still a live watcher: keep the stamp moving
        touch(current)
        return 0

    state = load_state()
    now = time.time()

    # a pause is already announced once, when the 429 arrived; polls during it
    # simply leave the label alone
    left = throttle_left()
    if left > 0:
        print("throttled: %ds left" % int(left))
        # the label is deliberately held, but the watcher is alive: without the
        # stamp the page would call a two-minute pause a dead task
        touch(current)
        return 0

    try:
        online = read_presence(args.member, args.expect)
    except Throttled:
        raise
    except Exception as error:  # network, timeout, non-200, unparsable, wrong member
        print("could not read presence: %s" % error, file=sys.stderr)
        note("failed: %s" % error)
        save_state(state)
        return 1

    label = settle_label(state, online, now, current.get("status"))
    save_state(state)

    log_sample("public", online, label, " streak=%s" % state.get("online_streak"))
    return settle(current, label, args, source="public")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Write status.json from IMVU presence.")
    parser.add_argument("--member", default=XR_MEMBER, help="IMVU member id (default: the XR member)")
    parser.add_argument("--expect", default=XR_NAME, help='username that member must have (default: "xr"; "" skips the check)')
    parser.add_argument("--dry-run", action="store_true", help="report the decision without writing")
    parser.add_argument("--watch", type=int, metavar="SECONDS", default=0, help="keep polling instead of running once")
    parser.add_argument(
        "--skip-self-check",
        action="store_true",
        help="do not run tools/test_track_status.py before watching",
    )
    args = parser.parse_args()

    if not args.skip_self_check:
        passed, summary = run_self_check()
        if not passed:
            # the scheduled task must not run a broken edit; a one-off run is how
            # you debug it, so that one only warns
            message = "self-check failed: %s — %s" % (
                summary,
                "not starting" if args.watch > 0 else "running anyway (--skip-self-check to silence)",
            )
            note(message)
            print(message, file=sys.stderr)
            if args.watch > 0:
                return 2

    if args.watch > 0:
        print("watching %s every %ss" % (args.member, args.watch))
        while True:
            started = time.time()
            try:
                poll(args)
            except Exception as error:  # never let one bad answer end the watch
                note("failed: %s" % error)
            # a slow network answer should not shorten the gap to the next look
            time.sleep(max(1.0, args.watch - (time.time() - started)))

    return poll(args)


if __name__ == "__main__":
    sys.exit(main())
