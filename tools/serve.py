#!/usr/bin/env python3
"""Preview server for the XR page: always serves the current CSS and products.

A plain `python -m http.server` lets the browser keep an old stylesheet, so an
edit can look like it never landed — that is what made the releases label appear
left-aligned when the CSS already said otherwise. This server removes the guess
work: when it answers a page it rewrites the stylesheet link to carry the file's
own modification time, so any change to styles.css produces a new URL and the
browser has to fetch it. The page itself is marked no-store, so the stamped HTML
is never reused either.

    python tools/serve.py            # http://127.0.0.1:4173
    python tools/serve.py --port 8000
    python tools/serve.py --supervise   # watchdog: restarts the server if it exits
    python tools/serve.py --ensure      # start it only if nothing is listening yet

Only the stylesheet is stamped, by name: that is the file edited constantly.
Everything else is served straight from disk with no-cache so it revalidates.
"""

import argparse
import functools
import http.server
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT_BUSY = 3
LOG_PATH = os.path.join(tempfile.gettempdir(), "xr-serve.log")
# one file of this size plus its .1 backup: about 512 KB in total, however long
# the server runs, since every request writes a line
LOG_MAX = 256 * 1024


def _rotate_log():
    """Move a full log aside once, keeping a single backup."""
    try:
        if os.path.getsize(LOG_PATH) < LOG_MAX:
            return
        backup = LOG_PATH + ".1"
        if os.path.exists(backup):
            os.remove(backup)
        os.replace(LOG_PATH, backup)
    except OSError:
        pass


def log(message):
    """Report without needing a console: pythonw.exe has no stdout at all."""
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        if sys.stderr is not None:
            sys.stderr.write(line)
            sys.stderr.flush()
        else:
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(line)
            _rotate_log()
    except (OSError, ValueError):
        pass
STYLESHEET = os.path.join(ROOT, "styles.css")
PRODUCTS = os.path.join(ROOT, "assets", "products.json")

# matches the link however it was written, with or without a previous stamp
LINK = re.compile(r'href="styles\.css(?:\?v=[^"]*)?"')
# the page reads this to date its own products request
META = re.compile(r'<meta name="products-version"[^>]*>')


def _mtime(path):
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return None


def stamp(html_text):
    """Point the page at this exact version of each file it reads."""
    version = _mtime(STYLESHEET)
    if version is not None:
        html_text = LINK.sub('href="styles.css?v=%d"' % version, html_text)

    # products.json is rewritten by the scraper every hour. Stamping it lets the
    # page ask for one stable URL and revalidate it, instead of building a new
    # timestamp every minute and downloading the whole list each time.
    products = _mtime(PRODUCTS)
    if products is not None:
        tag = '<meta name="products-version" content="%d" />' % products
        if META.search(html_text):
            html_text = META.sub(tag, html_text, count=1)
        else:
            html_text = html_text.replace("</head>", "  " + tag + "\n</head>", 1)
    return html_text


class Server(http.server.ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # listening, which would split requests between two servers (and answered
    # nothing useful here). Refuse the second bind instead.
    allow_reuse_address = os.name != "nt"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _refused(self):
        """Files that must never be downloadable, even if one is left behind.

        The session cookie is kept outside this folder, but a stray copy — an
        old paste, an editor backup, anything dot-prefixed — must not be one
        URL away from being handed out. The check runs on the decoded path: the
        handler unquotes `%2E` to `.` before it looks at the filesystem, so a
        rule reading the raw request line would let `/session%2Ecookies`
        straight through.
        """
        path = self.path.split("?", 1)[0]
        # decode until it stops changing, so double-encoded names cannot hide
        for _ in range(3):
            decoded = urllib.parse.unquote(path, errors="replace")
            if decoded == path:
                break
            path = decoded

        parts = path.replace("\\", "/").split("/")
        names = [part for part in parts if part and part not in (".", "..")]
        if any(part.startswith(".") for part in names):
            return True
        return bool(names) and names[-1].lower().endswith(".cookies")

    def do_GET(self):
        if self._refused():
            self.send_error(404)
            return
        if not self._serve_stamped():
            super().do_GET()

    def do_HEAD(self):
        if self._refused():
            self.send_error(404)
            return
        if not self._serve_stamped(head_only=True):
            super().do_HEAD()

    def _serve_stamped(self, head_only=False):
        """Answer an HTML page with the stylesheet stamped; False means 'not mine'."""
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not path.endswith(".html") or not os.path.isfile(path):
            return False

        with open(path, encoding="utf-8") as handle:
            body = stamp(handle.read()).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # the stamp must be rebuilt on every load, so the page itself is never kept
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)
        return True

    def end_headers(self):
        # static assets revalidate rather than being reused blind
        if self.path.split("?")[0].endswith((".css", ".js", ".json", ".jpg", ".png", ".webp", ".svg")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))


def listening(bind, port):
    """True when something already answers on that port."""
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((bind, port)) == 0


def ensure(port, bind):
    """Start a supervised server only if the port is idle, then return.

    This is the mode to run from a scheduler or a shortcut: it is a no-op when
    the preview is already up, and when it is not it launches the watchdog
    detached and exits, so the caller is never left holding a server.
    """
    if listening(bind, port):
        log("already serving on http://%s:%d" % (bind, port))
        return 0

    flags = 0
    if os.name == "nt":
        # own process group, no console of its own, and no tie to this caller
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--port", str(port), "--bind", bind, "--supervise"],
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    log("started a supervised server on http://%s:%d" % (bind, port))
    return 0


def serve(port, bind):
    handler = functools.partial(Handler)
    try:
        server = Server((bind, port), handler)
    except OSError as error:
        # something else already owns the port; that is a normal outcome for a
        # second instance, not a crash
        log("port %d is already taken (%s); standing down" % (port, error))
        return PORT_BUSY
    log("serving %s on http://%s:%d" % (ROOT, bind, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def supervise(port, bind):
    """Run the server as a child process and bring it back whenever it exits.

    The preview kept dropping because the process was torn down with whatever
    started it. Running detached from that parent and supervised here means a
    crash, a port hiccup or a kill is followed by a fresh server about a second
    later, so the panel recovers without anyone noticing.
    """
    command = [sys.executable, os.path.abspath(__file__), "--port", str(port), "--bind", bind]
    while True:
        started = time.time()
        code = subprocess.call(command)
        lived = time.time() - started
        if code == PORT_BUSY:
            # another server (or the task's earlier instance) holds the port
            log("another server owns port %d; watchdog stopping" % port)
            return 0
        log("server exited with %s after %.0fs; restarting" % (code, lived))
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Serve the page with a stamped stylesheet.")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--supervise", action="store_true", help="watchdog the server and restart it if it exits")
    parser.add_argument("--ensure", action="store_true", help="start a supervised server only if the port is idle")
    args = parser.parse_args()

    if args.ensure:
        return ensure(args.port, args.bind)
    if args.supervise:
        return supervise(args.port, args.bind)
    return serve(args.port, args.bind)


if __name__ == "__main__":
    sys.exit(main())
