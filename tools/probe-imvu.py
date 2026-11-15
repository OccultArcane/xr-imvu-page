#!/usr/bin/env python3
"""Ask IMVU, from wherever this runs, for everything the hourly refresh needs.

    python tools/probe-imvu.py

The refresh tools read IMVU from whichever machine hosts them, so moving them to
a GitHub runner asks a question no local test can answer: does IMVU serve that
network the same way? This makes the requests an hourly run makes *and* puts the
answers through the same parsers it uses, so a green run means both halves can
do their work from here rather than merely that IMVU is reachable:

  * the shop page, parsed by `tools/refresh-products.py`'s own `parse_cards()` -- a
    page that answers 200 but no longer carries the product card markup that
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


def row(label, status, ctype, size, took, verdict):
    print("%-36s %6s %-18s %9d %6.1fs  %s" % (
        label, status if status is not None else "-", ctype[:18] or "-", size, took, verdict))
    return 0 if verdict == "ok" else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    manufacturer = shop_manufacturer()
    print("from: %s" % sys.platform)
    print("%-36s %6s %-18s %9s %7s  %s" % ("target", "status", "content-type", "bytes", "time", "verdict"))

    failures = 0

    # ---- the product half: the shop page has to pass the refresh's own parser
    shop_url = SHOP_URL % manufacturer
    status, ctype, size, took, body = get(shop_url)
    products = []
    if status is None:
        failures += row("shop page", status, ctype, size, took, "FAILED: %s" % body)
    elif not (200 <= status < 300):
        failures += row("shop page", status, ctype, size, took, "FAILED: HTTP %d" % status)
    elif not size:
        failures += row("shop page", status, ctype, size, took, "FAILED: empty body")
    else:
        try:
            products = product_tool().parse_cards(body.decode("utf-8", "replace"))
            verdict = "ok" if products else "FAILED: no product card the parser reads"
        except Exception as error:  # Pillow missing, or the module moved
            verdict = "FAILED: parser unusable: %s" % error
        failures += row("shop page", status, ctype, size, took, verdict)

    if products:
        newest = products[0]
        print("  parsed: %s  %s  %d credits" % (newest["id"], newest["name"], newest["price"]))
        # the listing image the thumbnail is built from, same URL the refresh fetches
        status, ctype, size, took, body = get(newest["image"])
        if status is None:
            verdict = "FAILED: %s" % body
        elif not (200 <= status < 300):
            verdict = "FAILED: HTTP %d" % status
        elif not size:
            verdict = "FAILED: empty body"
        elif not ctype.startswith("image/"):
            verdict = "FAILED: served %s" % (ctype or "no type")
        else:
            verdict = "ok"
        failures += row("listing image: " + newest["id"], status, ctype, size, took, verdict)

    # ---- the creator half: the profile pages, then the pictures they lead to
    cards = creator_cards()
    for name, _src in cards:
        status, ctype, size, took, body = get(AVATAR_PAGE % name)
        if status is None:
            verdict = "FAILED: %s" % body
        elif not (200 <= status < 300):
            verdict = "FAILED: HTTP %d" % status
        elif b"My avatar page" not in (body if isinstance(body, bytes) else b""):
            verdict = "FAILED: no profile markup in the body"
        else:
            verdict = "ok"
        failures += row("avatar page: " + name, status, ctype, size, took, verdict)

    for name, src in cards:
        status, ctype, size, took, _body = get(src)
        if status is None:
            verdict = "FAILED: %s" % _body
        elif not (200 <= status < 300):
            verdict = "FAILED: HTTP %d" % status
        elif not size:
            verdict = "FAILED: empty body"
        elif not ctype.startswith("image/"):
            verdict = "FAILED: served %s" % (ctype or "no type")
        else:
            verdict = "ok"
        failures += row("picture: " + name, status, ctype, size, took, verdict)

    total = 2 + len(cards) * 2 if products else 1 + len(cards) * 2
    print("\n%d target(s) checked, %d failed" % (total, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
