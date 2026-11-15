#!/usr/bin/env python3
"""Tests for the picture rule in tools/creator-lookup.py.

The card shows one picture and it has to be the creator's own, addressed by the
id that drives the shop link -- otherwise `tools/refresh-creators.py` cannot
follow the card back to anyone. The profile usually hands back such a picture
(an animated avatar as a .gif), but a Snap photo carries no id at all, and an
id found in a picture does not prove it is *this* creator's.

Nothing here touches the network: the rule takes the member id and the picture
URL as text, so these feed it the shapes IMVU returns.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "creator-lookup.py")

GIF = "https://userimages-akm.imvu.com/catalog/includes/modules/phpbb2/images/avatars/%s_20855017476ad93b5528ced.gif"
JPG = "https://userimages-akm.imvu.com/catalog/includes/modules/phpbb2/images/avatars/%s_16374717496ae390cee7cca.jpg"
CID = "https://www.imvu.com/catalog/image_avatar.php?cid=%s"
SNAP = "https://userimages01-akm.imvu.com/userdata/45/20/49/47/userpics/Snap_IvOqnYTnrE2021497658.jpg"


def load():
    spec = importlib.util.spec_from_file_location("creator_lookup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReadingTheIdOutOfAPicture(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_both_url_forms_are_read(self):
        self.assertEqual(self.module.picture_id(GIF % "119374898"), "119374898")
        self.assertEqual(self.module.picture_id(CID % "119374898"), "119374898")

    def test_a_snap_photo_has_no_id(self):
        self.assertEqual(self.module.picture_id(SNAP), "")


class ChoosingWhatTheCardShows(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_this_creators_animated_picture_is_used_as_is(self):
        # keeping the animated file is the whole point: the id endpoint never moves
        self.assertEqual(self.module.card_picture("179356122", GIF % "179356122"), GIF % "179356122")

    def test_this_creators_still_picture_is_used_as_is(self):
        self.assertEqual(self.module.card_picture("114942714", JPG % "114942714"), JPG % "114942714")

    def test_a_picture_without_an_id_falls_back_to_the_id_endpoint(self):
        self.assertEqual(self.module.card_picture("119374898", SNAP), CID % "119374898")

    def test_another_members_picture_is_not_used(self):
        # the avatar page carries other members' thumbnails; one of theirs must
        # never end up on this card
        self.assertEqual(self.module.card_picture("179356122", GIF % "267761777"), CID % "179356122")


class TheCardItPrints(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_the_card_shows_the_picture_and_carries_no_shipped_copy(self):
        markup = self.module.card("G59", "https://example.invalid/shop", GIF % "119374898")

        self.assertIn('src="%s"' % (GIF % "119374898"), markup)
        self.assertIn(">G59<", markup)
        self.assertNotIn("data-live", markup)
        self.assertNotIn("assets/creators/", markup)


if __name__ == "__main__":
    unittest.main(verbosity=2)
