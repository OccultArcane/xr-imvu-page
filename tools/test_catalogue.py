#!/usr/bin/env python3
"""Tests for tools/list-products.py against fake shop pages.

The live shop is public, so this could hit it — but a test that depends on how
many products the shop has today would break the moment one is added. These feed
the walk pages of the same shape instead and check the paging rules: stop at the
first empty page, drop a listing that appears twice, stop when a page adds
nothing new, and write the catalogue in one atomic file.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "list-products.py")


def card(pid, name):
    return (
        '<div class="widget-product-wide" id="%s"><a title="%s">'
        '<img src="https://cdn.imvu.com/productdata/%s.png">'
        '<span class="price"><span class=\'notranslate\'>724 credits</span></span></div>'
        % (pid, name, pid)
    )


def load():
    spec = importlib.util.spec_from_file_location("list_products", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WalkingTheShop(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.pages = {}
        self.module.shop.fetch = self.fetch
        self.module.time.sleep = lambda _s: None

    def fetch(self, url):
        page = int(url.rsplit("page=", 1)[1])
        return self.pages.get(page, "")

    def test_it_walks_to_the_first_empty_page(self):
        self.pages = {1: card("111", "xr one") + card("222", "xr two"),
                      2: card("333", "xr three")}

        found = self.module.every_product("106440837", 40, 0)

        self.assertEqual([p["id"] for p in found], ["111", "222", "333"])

    def test_a_listing_seen_twice_is_kept_once(self):
        self.pages = {1: card("111", "xr one") + card("222", "xr two"),
                      2: card("222", "xr two") + card("333", "xr three")}

        found = self.module.every_product("106440837", 40, 0)

        self.assertEqual([p["id"] for p in found], ["111", "222", "333"])

    def test_a_page_that_adds_nothing_ends_the_walk(self):
        # the shop repeating itself must not be read forever
        self.pages = {1: card("111", "xr one"), 2: card("111", "xr one"),
                      3: card("999", "xr never")}

        found = self.module.every_product("106440837", 40, 0)

        self.assertEqual([p["id"] for p in found], ["111"])

    def test_max_pages_is_a_ceiling(self):
        self.pages = {n: card(str(n) * 3, "xr %d" % n) for n in range(1, 6)}

        found = self.module.every_product("106440837", 2, 0)

        self.assertEqual(len(found), 2)

    def test_main_writes_the_catalogue_with_a_product_url(self):
        self.pages = {1: card("111", "xr one")}
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(os.unlink, path)

        self.module.sys.argv = ["list-products.py", "--out", path, "--quiet", "--delay", "0"]
        code = self.module.main()

        self.assertEqual(code, 0)
        with open(path, encoding="utf-8") as opened:
            written = json.load(opened)
        self.assertEqual([p["id"] for p in written], ["111"])
        self.assertEqual(written[0]["name"], "xr one")
        self.assertEqual(written[0]["price"], 724)
        self.assertTrue(written[0]["url"].endswith("products_id=111"))

    def test_an_empty_shop_writes_nothing(self):
        self.module.sys.argv = ["list-products.py", "--out", os.devnull, "--quiet"]
        self.assertEqual(self.module.main(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
