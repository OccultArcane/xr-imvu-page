#!/usr/bin/env python3
"""List every product in the shop, in the order the shopfront shows them.

The shopfront is public, server-rendered HTML — no session, no browser needed, and
no CORS problem because this is Python. `tools/refresh-products.py` already reads
it, but only far enough to fill the releases row: it stops after eight pages and
writes the newest handful into assets/products.json. This walks the same pages to
the end instead and writes the whole catalogue, which is what a bulk pass over
every product needs.

    python tools/list-products.py                 # the XR shop, every page
    python tools/list-products.py --max-pages 5   # stop early
    python tools/list-products.py --out assets/catalogue.json

Output is a JSON list of {id, name, price, url, image}, newest first as the shop
shows it, plus every id on stdout so it can be handed straight to the embedding
script:

    python tools/embed-page-in-products.py --catalogue assets/catalogue.json

Exit code is 0 when something was written, 1 when the shop could not be read.
"""

import argparse
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_refresher():
    """The existing shop reader, for its fetch headers and card parser.

    Loading by path because the file name has a dash in it; one implementation of
    "what a product card looks like" is better than two that can drift.
    """
    spec = importlib.util.spec_from_file_location(
        "xr_shop_refresh", os.path.join(HERE, "refresh-products.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shop = load_refresher()


def every_product(manufacturer, max_pages, delay, log=print):
    """Cards from every page of the shop, deduplicated, in the order shown."""
    found = []
    seen = set()
    page = 1
    while page <= max_pages:
        url = shop.SHOP_URL.format(mid=manufacturer, page=page)
        cards = shop.parse_cards(shop.fetch(url))
        if not cards:
            log("page %d: empty, so that is the end of the shop" % page)
            break
        fresh = [card for card in cards if card["id"] not in seen]
        for card in fresh:
            seen.add(card["id"])
            found.append(card)
        log("page %d: %d card(s), %d new" % (page, len(cards), len(fresh)))
        if not fresh:
            # a page that adds nothing means the shop stopped paging; going on
            # would re-read the same listings forever
            log("page %d repeated what was already found, stopping" % page)
            break
        page += 1
        if page <= max_pages:
            time.sleep(max(0.0, delay))
    return found


def main():
    parser = argparse.ArgumentParser(description="List every product in the IMVU shop.")
    parser.add_argument("--manufacturer", default=shop.XR_MANUFACTURER,
                        help="IMVU manufacturer id (default: the XR shop)")
    parser.add_argument("--out", default=os.path.join("assets", "catalogue.json"),
                        help="where to write the catalogue (default: assets/catalogue.json)")
    parser.add_argument("--max-pages", type=int, default=40,
                        help="how many shop pages to walk at most (default: 40)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between page reads (default: 1)")
    parser.add_argument("--quiet", action="store_true", help="only the ids")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    log = (lambda _line: None) if args.quiet else print
    try:
        products = every_product(args.manufacturer, args.max_pages, args.delay, log)
    except Exception as error:  # network, timeout, unparseable page
        print("could not read the shop: %s" % error)
        return 1

    if not products:
        print("the shop returned no products at all; nothing written")
        return 1

    for product in products:
        product["url"] = shop.PRODUCT_URL.format(pid=product["id"])

    out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    temporary = out + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(products, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, out)

    print("%d product(s) written to %s" % (len(products), out))
    print("ids: %s" % " ".join(product["id"] for product in products))
    return 0


if __name__ == "__main__":
    sys.exit(main())
