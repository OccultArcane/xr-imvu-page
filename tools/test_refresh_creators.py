#!/usr/bin/env python3
"""Tests for tools/refresh-creators.py, against fake avatar pages.

The live site is reachable, but a test that depends on what a creator's avatar
looks like today would change the moment they upload a new one. These feed the
same shaped pages instead and pin the two rules that matter: an animated file
beats a still one, and an animation that still works is never downgraded.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "refresh-creators.py")

GIF = "https://userimages-akm.imvu.com/catalog/includes/modules/phpbb2/images/avatars/%s_%s.gif"
JPG = "https://userimages-akm.imvu.com/catalog/includes/modules/phpbb2/images/avatars/%s_%s.jpg"
STILL = "https://www.imvu.com/catalog/image_avatar.php?cid=%s"


def load():
    spec = importlib.util.spec_from_file_location("refresh_creators", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry(name, shop, src):
    """One card as the tool reads it, plus the markup a rewrite works on."""
    return {
        "name": name,
        "shop": shop,
        "src": src,
        "markup": '<img class="creator__avatar" src="%s" alt="" />' % src,
    }


def row(*entries):
    return '<ul class="creator-list">\n%s</ul>\n' % "".join(
        '<li><a href="?manufacturers_id=%s">'
        '<span class="creator__bracket">[</span>%s<span class="creator__bracket">]</span>'
        "%s</a></li>\n" % (e["shop"], e["name"], e["markup"])
        for e in entries
    )


class PickingAPicture(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_an_animated_file_beats_a_still_one(self):
        page = '<img src="%s"><img src="%s">' % (JPG % ("119374898", "aaa"), GIF % ("119374898", "bbb"))

        self.assertEqual(self.module.picture_for("119374898", page), GIF % ("119374898", "bbb"))

    def test_only_this_creators_own_file_counts(self):
        # the page carries a strip of other members: nothing of theirs is used,
        # and a page with no file of theirs at all says so the same way
        strip = '<img src="%s">' % (GIF % ("267761777", "ccc"))

        self.assertIsNone(self.module.picture_for("119374898", strip))
        self.assertIsNone(self.module.picture_for("119374898", "<html>nothing</html>"))


class ChoosingWhatACardShows(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def page(self, html):
        return lambda _url: html

    def test_the_creators_own_file_is_used(self):
        card = entry("G59", "119374898", STILL % "119374898")
        page = self.page('<img src="%s">' % (GIF % ("119374898", "bbb")))

        self.assertEqual(self.module.resolve(card, opener=page), GIF % ("119374898", "bbb"))

    def test_a_working_animation_is_kept_when_the_page_shows_nothing(self):
        # Numen's profile carries no file the tool can read; their gif is current
        card = entry("Numen", "179356122", GIF % ("179356122", "ddd"))

        kept = self.module.resolve(card, opener=self.page("<html></html>"), ping=lambda _u: True)

        self.assertEqual(kept, GIF % ("179356122", "ddd"))

    def test_a_dead_animation_falls_back_to_the_id_endpoint(self):
        card = entry("Numen", "179356122", GIF % ("179356122", "ddd"))

        fell_back = self.module.resolve(card, opener=self.page("<html></html>"), ping=lambda _u: False)

        self.assertEqual(fell_back, STILL % "179356122")

    def test_an_unreadable_page_still_falls_back_cleanly(self):
        card = entry("scissors", "345204947", STILL % "345204947")

        def broken(_url):
            raise OSError("offline")

        self.assertEqual(self.module.resolve(card, opener=broken), STILL % "345204947")


class RewritingThePage(unittest.TestCase):
    def test_only_the_cards_that_moved_are_rewritten(self):
        html = row(
            entry("G59", "119374898", STILL % "119374898"),
            entry("chokinglotus", "272737258", STILL % "272737258"),
        )

        def opener(url):
            if url.endswith("G59"):
                return '<img src="%s">' % (GIF % ("119374898", "bbb"))
            return "<html></html>"

        updated, moved = load().refresh(html, opener=opener)

        self.assertEqual([name for name, _wanted in moved], ["G59"])
        self.assertEqual(updated.count(GIF % ("119374898", "bbb")), 1)
        # the card's own attributes survive the rewrite, and the untouched card
        # is byte-identical
        self.assertIn('<img class="creator__avatar" src="%s" alt="" />' % (GIF % ("119374898", "bbb")), updated)
        self.assertIn(STILL % "272737258", updated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
