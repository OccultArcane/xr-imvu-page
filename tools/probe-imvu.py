#!/usr/bin/env python3
"""Ask IMVU, from wherever this runs, for everything the hourly refresh needs.

    python tools/probe-imvu.py

The refresh tools read IMVU from whichever machine hosts them, so moving them to
a GitHub runner asks a question no local test can answer: does IMVU serve that
network the same way? This makes the requests an hourly run makes *and* puts the
answers through the same parsers it uses, so a green run means both halves can
do their work from here rather than merely that IMVU is reachable:

  * the shop page, parsed by `tools/refresh-products.py`'s own `parse_cards()` --
    a page that answers 200 but no longer carries the product card markup that
    parser needs would still leave the releases row stale;
  * one product's listing image, the 100x80 original the thumbnail is built from;
  * the five avatar pages and the five pictures the creator row loads.

It writes nothing and changes nothing. Exit code is 0 only if every request came
back usable and both parsers found what they look for, so a run that was quietly
blocked shows red instead of green. Needs Pillow, because the product parser
lives in a module that imports it.
"""

import importlib.util
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


def product_tool():
    """The product refresh itself, so the probe parses with its code, not a copy."""
    path = os.path.join(ROOT, "tools", "refresh-products.py")
    spec = importlib.util.spec_from_file_location("refresh_products", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    """Status, content type, byte count, time and body of one GET, however it lands."""
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


def looks_like_picture(_body, ctype):
    """A listing or avatar picture: an image, whatever the size."""
    return None if ctype.startswith("image/") else "served %s" % (ctype or "no type")


def looks_like_profile(body, _ctype):
    """An avatar page: the real one carries this marker, a block page does not."""
    return None if b"My avatar page" in body else "no profile markup in the body"


class Probe:
    """One target at a time: fetch it, apply the shared verdict, report the row.

    Every target is judged the same way before its own check runs -- reachable,
    a 2xx, and a body with something in it -- so those three live here instead of
    being repeated at each call site. `expect` supplies whatever the target needs
    beyond that and returns None when the answer is usable, or the reason it is not.
    """

    def __init__(self):
        self.checked = 0
        self.failures = 0

    def check(self, label, url, expect=None):
        """Print the row for one target and return its body (or the error text)."""
        status, ctype, size, took, body = get(url)
        self.checked += 1

        if status is None:
            reason = body
        elif not 200 <= status < 300:
            reason = "HTTP %d" % status
        elif not size:
            reason = "empty body"
        else:
            reason = expect(body, ctype) if expect else None

        if reason:
            self.failures += 1
        print("%-36s %6s %-18s %9d %6.1fs  %s" % (
            label,
            status if status is not None else "-",
            ctype[:18] or "-",
            size,
            took,
            "ok" if reason is None else "FAILED: %s" % reason,
        ))
        return body


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    manufacturer = shop_manufacturer()
    probe = Probe()
    print("from: %s" % sys.platform)
    print("%-36s %6s %-18s %9s %7s  %s" % ("target", "status", "content-type", "bytes", "time", "verdict"))

    # ---- the product half: the shop page has to pass the refresh's own parser
    products = []

    def parse(body, _ctype):
        try:
            products.extend(product_tool().parse_cards(body.decode("utf-8", "replace")))
        except Exception as error:  # Pillow missing, or the module moved
            return "parser unusable: %s" % error
        return None if products else "no product card the parser reads"

    probe.check("shop page", SHOP_URL % manufacturer, expect=parse)

    if products:
        newest = products[0]
        print("  parsed: %s  %s  %d credits" % (newest["id"], newest["name"], newest["price"]))
        # the listing image the thumbnail is built from, same URL the refresh fetches
        probe.check("listing image: " + newest["id"], newest["image"], expect=looks_like_picture)

    # ---- the creator half: the profile pages, then the pictures they lead to
    cards = creator_cards()
    for name, _src in cards:
        probe.check("avatar page: " + name, AVATAR_PAGE % name, expect=looks_like_profile)
    for name, src in cards:
        probe.check("picture: " + name, src, expect=looks_like_picture)

    print("\n%d target(s) checked, %d failed" % (probe.checked, probe.failures))
    return 1 if probe.failures else 0


if __name__ == "__main__":
    sys.exit(main())
