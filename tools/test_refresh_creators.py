#!/usr/bin/env python3
"""Tests for tools/refresh-creators.py, against fake avatar pages.

The live site is reachable, but a test that depends on what a creator's avatar
looks like today would change the moment they upload a new one. These feed the
same shaped pages instead and check the two rules that matter: an animated file
beats a still one, and a card that already points at a working animation is not
downgraded to the static id endpoint when a profile page shows nothing.
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


def card(name, shop, src):
    return {
        "name": name,
        "shop": shop,
        "src": src,
        "markup": '<img class="creator__avatar" src="%s" alt="" />' % src,
    }


def row(name, shop, src):
    """One card as it stands in the page, for the rewriting cases."""
    return (
        '<li><a href="?manufacturers_id=%s">'
        '<span class="creator__bracket">[</span>%s<span class="creator__bracket">]</span>'
        '<img class="creator__avatar" src="%s" alt="" /></a></li>\n' % (shop, name, src)
    )


class PickingAPicture(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_an_animated_file_beats_a_still_one(self):
        page = '<img src="%s"><img src="%s">' % (JPG % ("119374898", "aaa"), GIF % ("119374898", "bbb"))

        picked = self.module.picture_for("119374898", page)

        self.assertEqual(picked, GIF % ("119374898", "bbb"))

    def test_another_members_picture_is_never_picked(self):
        # the profile page carries a strip of other members: only the id matches
        page = '<img src="%s">' % (GIF % ("267761777", "ccc"))

        self.assertIsNone(self.module.picture_for("119374898", page))

    def test_a_page_with_no_file_of_theirs_says_so(self):
        self.assertIsNone(self.module.picture_for("119374898", "<html>nothing</html>"))


class ResolvingACard(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def page(self, html):
        return lambda _url: html

    def test_the_creators_own_file_is_used(self):
        card_in = card("G59", "119374898", STILL % "119374898")

        wanted = self.module.resolve(card_in, opener=self.page('<img src="%s">' % (GIF % ("119374898", "bbb"))))

        self.assertEqual(wanted, GIF % ("119374898", "bbb"))

    def test_a_working_animation_is_kept_when_the_page_shows_nothing(self):
        # Numen's profile carries no file the tool can read; their gif is current
        card_in = card("Numen", "179356122", GIF % ("179356122", "ddd"))

        wanted = self.module.resolve(card_in, opener=self.page("<html></html>"), ping=lambda _u: True)

        self.assertEqual(wanted, GIF % ("179356122", "ddd"))

    def test_a_dead_animation_falls_back_to_the_id_endpoint(self):
        card_in = card("Numen", "179356122", GIF % ("179356122", "ddd"))

        wanted = self.module.resolve(card_in, opener=self.page("<html></html>"), ping=lambda _u: False)

        self.assertEqual(wanted, STILL % "179356122")

    def test_an_unreadable_page_still_falls_back_cleanly(self):
        def broken(_url):
            raise OSError("offline")

        card_in = card("scissors", "345204947", STILL % "345204947")

        self.assertEqual(self.module.resolve(card_in, opener=broken), STILL % "345204947")


class RewritingThePage(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_only_the_cards_that_moved_are_rewritten(self):
        html = (
            '<ul class="creator-list">\n'
            + row("G59", "119374898", STILL % "119374898")
            + row("chokinglotus", "272737258", STILL % "272737258")
            + "</ul>\n"
        )

        def opener(url):
            if url.endswith("G59"):
                return '<img src="%s">' % (GIF % ("119374898", "bbb"))
            return "<html></html>"

        updated, moved = self.module.refresh(html, opener=opener, quiet=True)

        self.assertEqual([name for name, _old, _new in moved], ["G59"])
        self.assertIn('src="%s"' % (GIF % ("119374898", "bbb")), updated)
        self.assertIn('src="%s"' % (STILL % "272737258"), updated)

    def test_a_current_row_is_left_untouched(self):
        html = (
            '<ul class="creator-list">\n'
            + row("G59", "119374898", GIF % ("119374898", "bbb"))
            + "</ul>\n"
        )

        updated, moved = self.module.refresh(
            html,
            opener=lambda _url: '<img src="%s">' % (GIF % ("119374898", "bbb")),
            quiet=True,
        )

        self.assertEqual(moved, [])
        self.assertEqual(updated, html)

    def test_the_page_keeps_its_other_attributes(self):
        html = (
            '<ul class="creator-list">\n'
            + row("G59", "119374898", STILL % "119374898")
            + "</ul>\n"
        )

        updated, _moved = self.module.refresh(
            html,
            opener=lambda _url: '<img src="%s">' % (GIF % ("119374898", "bbb")),
            quiet=True,
        )

        self.assertIn('class="creator__avatar"', updated)
        self.assertIn('alt=""', updated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
