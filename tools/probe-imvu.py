#!/usr/bin/env python3
"""Ask IMVU, from wherever this runs, for everything the hourly refresh needs.

    python tools/probe-imvu.py

The refresh tools read IMVU from whichever machine hosts them. Moving them to a
GitHub runner therefore asks a question no local test can answer: does IMVU
serve that network the same way? This makes exactly the requests an hourly run
would -- the shop page, the five avatar pages, and the five pictures the page
loads -- and prints a status code and byte count for each, plus whether the
body actually looks like the page rather than a challenge or an error served
with a 200.

It writes nothing and changes nothing. Exit code is 0 only if every request came
back usable, so a run that was quietly blocked shows red instead of green.
"""

import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
    ),
    "Referer": "https://www.imvu.com/",
}

SHOP_URL = "https://www.imvu.com/shop/web_search.php?manufacturers_id=%s"
AVATAR_PAGE = "https://avatars.imvu.com/%s"

LIST = re.compile(r'<ul class="creator-list">(.*?)</ul>', re.S)
CARD = re.compile(r"<li>(.*?)</li>", re.S)
NAME = re.compile(r'\[</span>([^<]+)<span class="creator__bracket"')
SRC = re.compile(r'src="([^"]+)"')


def creator_cards():
    """The row as the page has it: each creator's name and the picture it loads."""
    with open(ROOT + "index.html", encoding="utf-8") as handle:
        html = handle.read()
    row = LIST.search(html)
    found = []
    for block in CARD.findall(row.group(1)) if row else []:
        name, src = NAME.search(block), SRC.search(block)
        if name and src:
            found.append((name.group(1).strip(), src.group(1)))
    return found


def shop_manufacturer():
    """The shop the product refresh reads, taken from the tool that reads it."""
    with open(ROOT + "tools/refresh-products.py", encoding="utf-8") as handle:
        found = re.search(r'XR_MANUFACTURER\s*=\s*"(\d+)"', handle.read())
    return found.group(1) if found else "106440837"


def get(url):
    """Status, content type, byte count and body of one GET, however it lands."""
    started = time.monotonic()
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body, status = response.read(), response.status
            ctype = response.headers.get("content-type", "")
    except urllib.error.HTTPError as error:
        body, status = error.read(), error.code
        ctype = error.headers.get("content-type", "") if error.headers else ""
    except Exception as error:  # DNS, TLS, timeout, connection reset
        return None, "", 0, time.monotonic() - started, str(error)
    return status, ctype, len(body), time.monotonic() - started, body


def said(body):
    return body if isinstance(body, bytes) else b""


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    manufacturer = shop_manufacturer()
    cards = creator_cards()
    targets = [("shop page", SHOP_URL % manufacturer, b"manufacturers_id")]
    for name, _src in cards:
        targets.append(("avatar page: " + name, AVATAR_PAGE % name, b"My avatar page"))
    for name, src in cards:
        targets.append(("picture: " + name, src, None))

    print("from: %s" % sys.platform)
    print("%-34s %6s %-18s %9s %7s  %s" % ("target", "status", "content-type", "bytes", "time", "verdict"))

    failures = 0
    for label, url, marker in targets:
        status, ctype, size, took, body = get(url)
        if status is None:
            verdict = "FAILED: %s" % body
        elif not (200 <= status < 300):
            verdict = "FAILED: HTTP %d" % status
        elif not size:
            verdict = "FAILED: empty body"
        elif marker and marker not in said(body):
            # a 200 is not enough: a challenge or a logged-out shell arrives this way
            verdict = "FAILED: no %r in the body" % marker.decode()
        elif marker is None and not ctype.startswith("image/"):
            verdict = "FAILED: served %s" % (ctype or "no type")
        else:
            verdict = "ok"
        if verdict != "ok":
            failures += 1
        print("%-34s %6s %-18s %9d %6.1fs  %s" % (
            label, status if status is not None else "-", ctype[:18] or "-", size, took, verdict))

    print("\n%d target(s) checked, %d failed" % (len(targets), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
