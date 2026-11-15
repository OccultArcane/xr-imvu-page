#!/usr/bin/env python3
"""Look up an IMVU username and print everything a creator card needs.

Adding a card by hand means hunting three separate IMVU pages for a manufacturer
id, a shop link and a picture, and the picture is the easy one to get wrong: the
avatar page also carries a strip of *other* members' thumbnails, so grabbing
"the first picture" puts a stranger's face on the card. This script reads the
member's own profile picture — the `avBigPic` image — and nothing else.

    python tools/creator-lookup.py scissors
    python tools/creator-lookup.py scissors --insert    # and add the card to index.html

Output is the id, the shop link, how many listings the shop holds, the picture
URL, and a ready-to-paste <li> block with the card's brackets, classes and img
attributes already in place.

The card shows that URL directly; no copy of the portrait is shipped beside the
page, so there is no stale picture for a newer one to be swapped over. When the
profile hands back a picture that does not carry the member id — a Snap photo
doesn't — the id-addressable `image_avatar.php?cid=` endpoint is used instead, so
`tools/refresh-creators.py` can still follow the card.

`--insert` drops the card at the end of the creator row in index.html; if the
creator is already there it says so and leaves the file untouched.

Exit code is 0 when the member and shop were found, 1 when the username could
not be read or is not a member.
"""

import argparse
import html
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")

# the row the card belongs in, and the tag that closes it
LIST_OPEN = re.compile(r'<ul class="creator-list">')

AVATAR_PAGE = "https://avatars.imvu.com/{name}"
SHOP_URL = "https://www.imvu.com/shop/web_search.php?manufacturers_id={mid}"
# the picture the card shows when the profile hands back one that carries no id,
# keyed by the member id so every card's URL matches its shop link
AVATAR_IMAGE = "https://www.imvu.com/catalog/image_avatar.php?cid={mid}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
    ),
    "Referer": "https://www.imvu.com/",
}

# the profile picture sits inside this block; the thumbnails below it are other
# members, which is why the search is anchored here rather than on the page
BIG_PIC_BLOCK = re.compile(r'id="avBigPic"(.*?)(?:id="avFriends"|</table>)', re.S)
BIG_PIC_IMG = re.compile(r'<img[^>]+src="(https?://[^\"]+)"')
MEMBER_ID = re.compile(r"olstatus_(\d+)_")
SHOP_NAME = re.compile(r"Visit (.+?)'s Homepage")
CARD_SPLIT = re.compile(r'(?=<div class="widget-product-wide)')
CARD_TITLE = re.compile(r'title="([^"]*)"')

# the member id as IMVU writes it into a picture URL: `?cid=` on the endpoint,
# or the filename prefix on the per-image files
CID = re.compile(r"[?&]cid=(\d+)")
FILENAME_ID = re.compile(r"^(\d+)_")


def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read().decode("utf-8", "replace")


def picture_id(url):
    """The member id a picture URL belongs to, or '' when it carries none."""
    found = CID.search(url)
    if found:
        return found.group(1)
    named = FILENAME_ID.match(os.path.basename(url))
    return named.group(1) if named else ""


def card_picture(mid, picture):
    """The URL the card shows, given the picture the profile handed back.

    The profile's own picture is already the creator's current one, and an
    animated avatar comes back as a .gif that keeps moving, so it is used as-is.
    A picture carrying no member id -- or somebody else's -- cannot be followed
    back to this creator by the refresh, so the id endpoint takes its place.
    """
    if picture_id(picture) == mid:
        return picture
    return AVATAR_IMAGE.format(mid=mid)


def member(name):
    """The member id and their own profile picture, from their avatar page."""
    try:
        page = fetch(AVATAR_PAGE.format(name=urllib.parse.quote(name)))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise LookupError("no member by that name (IMVU returned 404)")
        raise
    if "My avatar page" not in page:
        raise LookupError("that page is not an avatar page")

    # without this block there is nothing safe to read: the rest of the page is
    # a strip of other members, and taking "the first picture" from it is how a
    # stranger ends up on the card
    block = BIG_PIC_BLOCK.search(page)
    if not block:
        raise LookupError("%s's page has no profile picture block this tool can read" % name)
    window = block.group(1)
    picture = BIG_PIC_IMG.search(window)
    if not picture:
        raise LookupError("%s has no profile picture set" % name)

    mid = MEMBER_ID.search(window) or MEMBER_ID.search(page)
    if not mid:
        raise LookupError("%s has no member id on their page" % name)

    return mid.group(1), picture.group(1)


def shop(mid):
    """The shop's display name and listings, or None when this member has none.

    A member id and a manufacturer id are the same number only for members who
    actually sell, so the shop that answers may well belong to someone else.
    The display name is checked against the username for exactly that reason.
    """
    page = fetch(SHOP_URL.format(mid=mid))
    name = SHOP_NAME.search(page)
    cards = CARD_SPLIT.split(page)[1:]
    titles = []
    for chunk in cards:
        title = CARD_TITLE.search(chunk)
        if title:
            titles.append(html.unescape(title.group(1)).strip())
    return (name.group(1) if name else None), len(cards), titles


def card(name, link, src):
    """The markup block, indented to drop straight into .creator-list."""
    return """          <li>
            <a class="creator" href="{link}" target="_blank" rel="noopener noreferrer">
              <span class="creator__chip viewfinder">
                <img
                  class="creator__avatar"
                  src="{src}"
                  alt=""
                  width="52"
                  height="72"
                  loading="lazy"
                  referrerpolicy="no-referrer"
                />
                <span class="creator__name">
                  <span class="creator__bracket" aria-hidden="true">[</span>{name}<span class="creator__bracket" aria-hidden="true">]</span>
                </span>
              </span>
            </a>
          </li>""".format(link=link, src=src, name=name)


def insert_card(path, block, mid):
    """Drop the card at the end of the creator row.

    Writes through a temporary file so a half-written page is never left behind,
    and keeps the file's own line endings by reading and writing verbatim. A
    creator already in the row is reported rather than added twice, which makes
    the command safe to re-run.
    """
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()

    opening = LIST_OPEN.search(text)
    if not opening:
        raise LookupError('no <ul class="creator-list"> in %s' % os.path.basename(path))
    closing = text.find("</ul>", opening.end())
    if closing == -1:
        raise LookupError("the creator list is never closed in %s" % os.path.basename(path))

    row = text[opening.end():closing]
    if ("manufacturers_id=%s" % mid) in row:
        return False, 0

    # sit the block on its own line above the closing tag, matching the gap the
    # existing cards keep between them
    line_start = text.rfind("\n", 0, closing) + 1
    updated = text[:line_start] + "\n" + block + "\n" + text[line_start:]

    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    os.replace(temporary, path)

    return True, text.count("\n", 0, line_start) + 2


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Look up an IMVU creator's card details.")
    parser.add_argument("username", help="IMVU username, e.g. scissors")
    parser.add_argument("--insert", action="store_true", help="add the card to the creator row in index.html")
    parser.add_argument("--index", default=INDEX, help="page to edit with --insert (default: index.html)")
    args = parser.parse_args()

    name = args.username.strip()
    try:
        mid, picture = member(name)
    except Exception as error:
        print("could not read %s: %s" % (name, error), file=sys.stderr)
        return 1

    try:
        display, listings, titles = shop(mid)
    except Exception as error:
        print("could not read the shop: %s" % error, file=sys.stderr)
        display, listings, titles = None, 0, []

    # a shop under this number that answers to another name belongs to someone
    # else — the member simply has no store of their own
    owns_shop = bool(display) and display.lower() == name.lower()

    print("name      %s" % (display if owns_shop else name))
    print("member    %s" % mid)
    if owns_shop:
        print("shop      %s" % SHOP_URL.format(mid=mid))
        print("listings  %d" % listings)
        for title in titles[:5]:
            print("          %s" % title)
    else:
        print("shop      none (no storefront under this member id)")

    # the profile's own picture is used as-is: it is already the creator's
    # current one, and an animated avatar comes back as a .gif that keeps moving.
    # Only a picture that carries no member id is replaced, because then nothing
    # can follow the card back to this creator.
    src = card_picture(mid, picture)
    if src == picture:
        print("picture   %s" % picture)
    else:
        print("picture   %s (the profile's own carries no member id, so the card uses this)" % src)

    link = SHOP_URL.format(mid=mid) if owns_shop else AVATAR_PAGE.format(name=urllib.parse.quote(name))
    markup = card(name, link, src)

    if args.insert:
        try:
            changed, line = insert_card(args.index, markup, mid)
        except Exception as error:
            print("could not edit the page: %s" % error, file=sys.stderr)
            return 1
        if changed:
            print("inserted  %s line %d" % (os.path.basename(args.index), line))
        else:
            print("inserted  nothing — %s is already in the row" % name)
        return 0

    print("\n<snippet>")
    print(markup)
    print("</snippet>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
