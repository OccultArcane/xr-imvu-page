#!/usr/bin/env python3
"""Refresh the releases row from the IMVU shop.

IMVU serves its shopfront as server-rendered HTML and sends no CORS headers, so
a browser cannot read it. This script does the reading instead: it pulls the
newest listings for one manufacturer, rebuilds each thumbnail as a 300x240 WebP
(the CDN only offers 100x80 originals, so they are sharpened at that size and
enlarged once here instead of being stretched raw in every browser) and rewrites
assets/products.json atomically.

The page polls that file every minute, so running this on a schedule keeps the
releases row current without touching index.html.

    python tools/refresh-products.py              # the XR shop, newest 8
    python tools/refresh-products.py --count 12

Windows, every hour:

    schtasks /create /tn "XR releases" /sc hourly ^
      /tr "python %CD%\\tools\\refresh-products.py"

Exit code is 0 on write, 1 when the shop could not be read or parsed.
"""

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.request

from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
JSON_PATH = os.path.join(ASSETS, "products.json")
THUMB_DIR = os.path.join(ASSETS, "products")

XR_MANUFACTURER = "106440837"
SHOP_URL = "https://www.imvu.com/shop/web_search.php?manufacturers_id={mid}&page={page}"
PRODUCT_URL = "https://www.imvu.com/shop/product.php?products_id={pid}"

THUMB_SIZE = (300, 240)

# The originals are 100x80, so every thumbnail is a 3x enlargement and no amount
# of resampling invents detail. Edge contrast is the one thing that can be put
# back, and it works better before the resize than after: sharpening at 100x80
# hands LANCZOS stronger gradients to interpolate, which measured about 45% more
# detail at tile size than a single post-pass (276 vs 190 variance of the
# Laplacian) with only faint ringing, where a heavy post-pass leaves halos.
PRE_SHARPEN = (0.8, 100, 2)
POST_SHARPEN = (1.2, 80, 2)
THUMB_QUALITY = 86

# Bumped when the pipeline above changes: thumbnails carry it, so an old file is
# rebuilt even though its source bytes never moved.
THUMB_STYLE = 2
LOG_PATH = os.path.join(tempfile.gettempdir(), "xr-products.log")


def note(message):
    """One line of history per run: under pythonw there is no console to print to."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
    ),
    "Referer": "https://www.imvu.com/",
}

CARD_SPLIT = re.compile(r'(?=<div class="widget-product-wide)')
CARD_ID = re.compile(r'<div class="widget-product-wide[^"]*"[^>]*id="(\d+)"')
CARD_TITLE = re.compile(r'title="([^"]*)"')
CARD_IMAGE = re.compile(r'<img src="(https://[^"]+/productdata/[^"]+)"')
CARD_PRICE = re.compile(r'class="price"><span class=[\'"]notranslate[\'"]>([\d,]+)')


def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read().decode("utf-8", "replace")


def parse_cards(page):
    """Every complete product card on one shop page, in the order shown."""
    cards = []
    for chunk in CARD_SPLIT.split(page)[1:]:
        pid = CARD_ID.match(chunk)
        title = CARD_TITLE.search(chunk)
        image = CARD_IMAGE.search(chunk)
        price = CARD_PRICE.search(chunk)
        if not (pid and title and image and price):
            continue
        cards.append(
            {
                "id": pid.group(1),
                "name": html.unescape(title.group(1)).strip(),
                "price": int(price.group(1).replace(",", "")),
                "image": image.group(1),
            }
        )
    return cards


def newest(manufacturer, count):
    """The shop lists newest first, so page 1 usually holds everything asked for."""
    found = []
    page = 1
    while len(found) < count:
        cards = parse_cards(fetch(SHOP_URL.format(mid=manufacturer, page=page)))
        if not cards:
            break
        for card in cards:
            if all(card["id"] != seen["id"] for seen in found):
                found.append(card)
        page += 1
        if page > 8:
            break
    return found[:count]


def existing_items():
    """The list already on disk, by product id: the record of what was built last run."""
    try:
        with open(JSON_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {item.get("id"): item for item in data if isinstance(item, dict) and item.get("id")}


def download_image(card, previous):
    """(bytes or None, etag, last-modified). None means the CDN said 304."""
    headers = dict(HEADERS)
    if previous:
        # ask the CDN whether anything changed before sending the picture again
        if previous.get("imageEtag"):
            headers["If-None-Match"] = previous["imageEtag"]
        if previous.get("imageModified"):
            headers["If-Modified-Since"] = previous["imageModified"]

    request = urllib.request.Request(card["image"], headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return (
                response.read(),
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None, previous.get("imageEtag"), previous.get("imageModified")
        raise


def build_thumbnail(card, previous):
    """Rebuild the 300x240 WebP from the 100x80 original — but only if it changed.

    The expensive half of a refresh is the resize and the WebP encode, and
    rewriting eight files every hour churns their timestamps (and any
    browser cache keyed on them) for nothing. So the source is checksummed: if
    the bytes and the file on disk are both what they were last run, the file is
    left exactly as it is. A conditional request keeps the download cheap too
    when the CDN offers ETag or Last-Modified.
    """
    filename = card["id"] + ".webp"
    path = os.path.join(THUMB_DIR, filename)

    # a conditional request is only worth making when there is a local copy to
    # stand behind the answer: a 304 means "what you have is current", which is
    # useless if the file was deleted or the record never existed
    styled = bool(previous and previous.get("imageStyle") == THUMB_STYLE)
    local_ok = bool(styled and previous.get("imageHash") and os.path.exists(path))
    raw, etag, modified = download_image(card, previous if local_ok else None)
    if raw is None and local_ok:
        return filename, previous["imageHash"], etag, modified, False
    if raw is None:
        # an unexplained 304: ask again without conditions rather than trust it
        raw, etag, modified = download_image(card, None)
    if raw is None:
        raise LookupError("no image bytes for product %s" % card["id"])

    digest = hashlib.sha256(raw).hexdigest()
    if styled and previous.get("imageHash") == digest and os.path.exists(path):
        return filename, digest, etag, modified, False

    image = Image.open(io.BytesIO(raw)).convert("RGB")
    image = image.filter(ImageFilter.UnsharpMask(*PRE_SHARPEN))
    image = image.resize(THUMB_SIZE, Image.LANCZOS)
    image = image.filter(ImageFilter.UnsharpMask(*POST_SHARPEN))
    image.save(path, "WEBP", quality=THUMB_QUALITY, method=6)
    return filename, digest, etag, modified, True


def write_json(items):
    """Write beside the real file and swap it in, so a page never reads half a list."""
    temporary = JSON_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(items, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, JSON_PATH)


def main():
    # product names carry characters a Windows console codepage cannot encode
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Refresh products.json from the IMVU shop.")
    parser.add_argument("--manufacturer", default=XR_MANUFACTURER, help="IMVU manufacturer id")
    parser.add_argument("--count", type=int, default=8, help="how many newest listings to keep")
    args = parser.parse_args()

    try:
        cards = newest(args.manufacturer, args.count)
    except Exception as error:  # network, timeout, non-200
        print("could not read the shop: %s" % error, file=sys.stderr)
        note("failed: %s" % error)
        return 1

    if len(cards) < args.count:
        print("shop returned %d listings, wanted %d" % (len(cards), args.count), file=sys.stderr)

    os.makedirs(THUMB_DIR, exist_ok=True)
    previous = existing_items()
    keep = set()
    rebuilt = 0
    for card in cards:
        filename, digest, etag, modified, fresh = build_thumbnail(card, previous.get(card["id"]))
        rebuilt += 1 if fresh else 0
        keep.add(filename)
        card["src"] = "assets/products/" + filename
        card["url"] = PRODUCT_URL.format(pid=card["id"])
        card["imageStyle"] = THUMB_STYLE
        if digest:
            card["imageHash"] = digest
        if etag:
            card["imageEtag"] = etag
        if modified:
            card["imageModified"] = modified
        print("  %s  %s  %d credits%s" % (card["id"], card["name"], card["price"], "" if fresh else "  (image unchanged)"))

    if not cards:
        print("nothing to write", file=sys.stderr)
        return 1

    write_json(cards)

    for filename in os.listdir(THUMB_DIR):
        if filename.endswith(".webp") and filename not in keep:
            os.remove(os.path.join(THUMB_DIR, filename))

    print(
        "wrote %s with %d products (%d image%s rebuilt)"
        % (os.path.relpath(JSON_PATH, ROOT), len(cards), rebuilt, "" if rebuilt == 1 else "s")
    )
    note("wrote %d products, newest: %s" % (len(cards), cards[0]["name"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
