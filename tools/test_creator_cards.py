#!/usr/bin/env python3
"""Tests that every card in the creator row agrees with itself.

The row is the one place on the page where three independent facts have to match
per card: the shop link, the picture and the label. A hand swap that moves the
picture and the label but not the href leaves a chip linking one creator's shop
under another creator's name -- and the page still renders, still loops, still
looks right. Reading the rendered chips back is what caught it, so this checks
all three fields together instead.

The picture is IMVU's, not a copy: nothing is shipped in `assets/creators/` any
more, so a card pointing at a local file is a card that will go stale, and that
is reported too.

The real index.html is checked as it stands; the rest build small rows that are
deliberately crossed, to prove the check fails when it should.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")

LIST = re.compile(r'<ul class="creator-list">(.*?)</ul>', re.S)
CARD = re.compile(r"<li>(.*?)</li>", re.S)
NAME = re.compile(r'\[</span>([^<]+)<span class="creator__bracket"')
HREF = re.compile(r'manufacturers_id=(\d+)')
SRC = re.compile(r'<img[^>]*?(?<![-\w])src="([^"]+)"', re.S)

# The picture URL carries the member id in one of two ways: IMVU's own avatar
# endpoint takes it as `?cid=`, and the older per-image filenames put it before
# the first underscore. A card whose picture has neither cannot be matched to its
# shop, so that counts as a failure rather than a skip.
CID = re.compile(r"[?&]cid=(\d+)")
FILENAME_ID = re.compile(r"^(\d+)_")


def picture_id(url):
    """The member id the picture belongs to, or '' when the URL has none."""
    found = CID.search(url)
    if found:
        return found.group(1)
    named = FILENAME_ID.match(os.path.basename(url))
    return named.group(1) if named else ""


def cards(html):
    """Every card in the row, as the facts it has to keep in agreement."""
    row = LIST.search(html)
    if not row:
        return []
    found = []
    for block in CARD.findall(row.group(1)):
        name = NAME.search(block)
        href = HREF.search(block)
        src = SRC.search(block)
        found.append({
            "name": name.group(1).strip() if name else "",
            "shop": href.group(1) if href else "",
            "src": src.group(1) if src else "",
        })
    return found


def problems(html):
    """Everything wrong with the row, as sentences; empty means it all agrees."""
    found = cards(html)
    if not found:
        return ['no creator cards found: the row\'s markup has changed shape']

    trouble = []
    seen_shops = {}
    seen_pictures = {}
    seen_names = {}
    for card in found:
        where = card["name"] or "(unnamed card)"

        for field in ("name", "shop", "src"):
            if not card[field]:
                trouble.append("%s: no %s" % (where, field))
        if not all(card.values()):
            continue

        if not card["src"].startswith("https://"):
            trouble.append(
                "%s: shows a shipped copy (%s) instead of IMVU's own picture"
                % (where, card["src"])
            )

        picture = picture_id(card["src"])
        if not picture:
            trouble.append(
                "%s: the picture URL carries no member id (%s)" % (where, card["src"])
            )
        elif picture != card["shop"]:
            trouble.append(
                "%s: links to shop %s but shows member %s's picture"
                % (where, card["shop"], picture)
            )

        if card["shop"] in seen_shops:
            trouble.append(
                "%s: shop %s is already used by %s"
                % (where, card["shop"], seen_shops[card["shop"]])
            )
        seen_shops[card["shop"]] = where

        if card["src"] in seen_pictures:
            trouble.append(
                "%s: %s is already used by %s"
                % (where, card["src"], seen_pictures[card["src"]])
            )
        seen_pictures[card["src"]] = where

        # a crossed pair often leaves both labels on one name while the two
        # pictures keep their own ids: each card then agrees with itself and the
        # row as a whole is the only place the swap shows
        if card["name"] in seen_names:
            trouble.append(
                "%s: the name appears twice in the row (also on %s's card)"
                % (where, seen_names[card["name"]])
            )
        seen_names[card["name"]] = where

    return trouble


def card(name, shop, src):
    """One card in the row's own markup, for the cases below."""
    return (
        '<li>\n'
        '  <a class="creator" href="https://www.imvu.com/shop/web_search.php?manufacturers_id=%s" target="_blank" rel="noopener noreferrer">\n'
        '    <span class="creator__chip viewfinder">\n'
        '      <img class="creator__avatar" src="%s" alt="" width="52" height="72" />\n'
        '      <span class="creator__name">\n'
        '        <span class="creator__bracket" aria-hidden="true">[</span>%s<span class="creator__bracket" aria-hidden="true">]</span>\n'
        '      </span>\n'
        '    </span>\n'
        '  </a>\n'
        '</li>\n' % (shop, src, name)
    )


def row(*cards_html):
    return '<ul class="creator-list">\n%s</ul>\n' % "".join(cards_html)


LIVE_ID = "https://www.imvu.com/catalog/image_avatar.php?cid=%s"
GIF = "https://userimages-akm.imvu.com/catalog/includes/modules/phpbb2/images/avatars/%s_20348436876a9f21e11da5a.gif"


class TheCommittedRow(unittest.TestCase):
    def test_every_card_agrees_with_itself(self):
        with open(INDEX, encoding="utf-8") as handle:
            html = handle.read()

        trouble = problems(html)

        self.assertEqual(trouble, [], "\n".join(trouble))

    def test_the_row_is_not_empty(self):
        with open(INDEX, encoding="utf-8") as handle:
            html = handle.read()

        self.assertGreaterEqual(len(cards(html)), 5)

    def test_no_card_ships_its_own_copy_of_a_portrait(self):
        with open(INDEX, encoding="utf-8") as handle:
            html = handle.read()

        local = [c["src"] for c in cards(html) if not c["src"].startswith("https://")]

        self.assertEqual(local, [])


class WhenACardIsCrossed(unittest.TestCase):
    def test_a_link_and_its_label_swapped_apart_is_reported(self):
        # the exact shape of the first swap attempt: the pictures and the labels
        # moved, the hrefs did not, and both cards ended up reading "Numen"
        broken = row(
            card("Numen", "179356122", GIF % "119374898"),
            card("Numen", "119374898", GIF % "179356122"),
        )

        trouble = problems(broken)

        self.assertTrue(any("shows member" in line for line in trouble), trouble)
        self.assertTrue(any("appears twice" in line for line in trouble), trouble)

    def test_a_picture_without_an_id_is_reported(self):
        # the older Snap filenames carried no id, which is why they were replaced
        broken = row(
            card("G59", "119374898",
                 "https://userimages01-akm.imvu.com/userdata/45/20/49/47/userpics/Snap_IvOqnYTnrE2021497658.jpg"),
        )

        trouble = problems(broken)

        self.assertTrue(any("no member id" in line for line in trouble), trouble)

    def test_two_cards_under_one_name_is_reported(self):
        broken = row(
            card("Numen", "179356122", GIF % "179356122"),
            card("Numen", "119374898", GIF % "119374898"),
        )

        trouble = problems(broken)

        self.assertTrue(any("appears twice" in line for line in trouble), trouble)

    def test_a_label_pointing_at_the_wrong_picture_is_reported(self):
        broken = row(card("Numen", "179356122", GIF % "119374898"))

        trouble = problems(broken)

        self.assertTrue(any("shows member" in line for line in trouble), trouble)

    def test_a_shipped_copy_is_reported(self):
        # the old shape: a local portrait behind the live picture
        broken = row(card("ghost", "111111", "assets/creators/ghost.jpg"))

        trouble = problems(broken)

        self.assertTrue(any("shipped copy" in line for line in trouble), trouble)

    def test_a_field_missing_altogether_is_reported(self):
        broken = row(
            '<li><a class="creator" href="https://www.imvu.com/shop/web_search.php?manufacturers_id=111111">'
            '<img class="creator__avatar" alt="" />'
            '<span class="creator__name"><span class="creator__bracket">[</span>G59'
            '<span class="creator__bracket">]</span></span></a></li>'
        )

        trouble = problems(broken)

        self.assertTrue(any("no src" in line for line in trouble), trouble)

    def test_the_same_shop_twice_is_reported(self):
        broken = row(
            card("G59", "119374898", GIF % "119374898"),
            card("G59", "119374898", GIF % "119374898"),
        )

        trouble = problems(broken)

        self.assertTrue(any("already used by" in line for line in trouble), trouble)

    def test_a_correct_pair_says_nothing(self):
        good = row(
            card("G59", "119374898", GIF % "119374898"),
            card("chokinglotus", "272737258", LIVE_ID % "272737258"),
        )

        self.assertEqual(problems(good), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
