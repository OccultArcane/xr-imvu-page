#!/usr/bin/env python3
"""Keep every creator card's picture pointing at that creator's current one.

    python tools/refresh-creators.py            # rewrite the cards that moved
    python tools/refresh-creators.py --check     # report, change nothing

Each card shows one picture and it is IMVU's own, addressed by the creator's id:
no copy of the portrait is shipped, so nothing stale is left behind on the page
for a newer picture to be swapped over. The catch is that the animated avatars
are not addressable by id alone -- their filenames carry a token
(`avatars/119374898_20348436876a9f21e11da5a.gif`) that only the creator's avatar
page shows -- while `catalog/image_avatar.php?cid=<id>`, which *is* addressable
by id, always answers with a static 80x110 JPEG.

So the ids in each card are resolved back to a picture here, and the card's `src`
is rewritten only when it has actually moved. Run it on the same schedule as the
product refresh and the row follows the creators on its own.
"""

import argparse
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")

AVATAR_PAGE = "https://avatars.imvu.com/{name}"
AVATAR_IMAGE = "https://www.imvu.com/catalog/image_avatar.php?cid={mid}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
    ),
    "Referer": "https://www.imvu.com/",
}

LIST = re.compile(r'<ul class="creator-list">(.*?)</ul>', re.S)
CARD = re.compile(r"<li>(.*?)</li>", re.S)
NAME = re.compile(r'\[</span>([^<]+)<span class="creator__bracket"')
SHOP = re.compile(r'manufacturers_id=(\d+)')
# the avatar img is the only src in a card; data-src and friends are not it
SRC = re.compile(r'(?<![-\w])src="([^"]+)"')

# the avatar page carries other members' thumbnails too, so a file is only taken
# when its filename starts with this creator's own id
OWN_FILE = r'https?://[^"\'\s)]*/avatars/%s_([0-9a-f]+)\.(gif|jpg|jpeg|png)'

# an animated file already in a card, and the id it belongs to
ANIMATED_FILE = re.compile(r"/avatars/(\d+)_[0-9a-f]+\.gif$", re.IGNORECASE)


def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read().decode("utf-8", "replace")


def alive(url):
    """Whether a picture URL still answers, without downloading it."""
    try:
        request = urllib.request.Request(url, headers=HEADERS, method="HEAD")
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except Exception:
        return False


def picture_for(mid, page):
    """The creator's own picture on their avatar page, animated file first.

    An animated avatar is served as .gif; a still one as .jpg or .png. Returns
    None when the page shows no file of theirs -- some profiles are private, and
    some accounts upload a Snap photo that carries no id.
    """
    found = {}
    for token, extension in re.findall(OWN_FILE % re.escape(mid), page, re.IGNORECASE):
        found.setdefault(extension.lower(), token)
    if not found:
        return None
    extension = "gif" if "gif" in found else sorted(found)[0]
    token = found[extension]
    return (
        "https://userimages-akm.imvu.com/catalog/includes/modules/phpbb2/images/"
        "avatars/%s_%s.%s" % (mid, token, extension)
    )


def cards(html):
    """Every card as (name, shop id, picture url, the whole card's markup)."""
    row = LIST.search(html)
    if not row:
        return []
    out = []
    for block in CARD.findall(row.group(1)):
        name = NAME.search(block)
        shop = SHOP.search(block)
        src = SRC.search(block)
        if name and shop and src:
            out.append({
                "name": name.group(1).strip(),
                "shop": shop.group(1),
                "src": src.group(1),
                "markup": block,
            })
    return out


def resolve(card, opener=fetch, ping=alive):
    """The URL this card's picture should point at, following its id.

    A file found on the creator's own page wins. Failing that, an animated file
    the card already has is kept as long as it still answers for this id -- that
    matters because the id endpoint's JPEG is static, and falling back to it
    would trade a working animation for a picture that never moves. Only when
    neither exists does the id-addressable JPEG take over.
    """
    try:
        page = opener(AVATAR_PAGE.format(name=urllib.parse.quote(card["name"])))
    except Exception:
        page = ""

    found = picture_for(card["shop"], page)
    if found:
        return found

    current = card["src"]
    mine = ANIMATED_FILE.search(current)
    if mine and mine.group(1) == card["shop"] and ping(current):
        return current

    return AVATAR_IMAGE.format(mid=card["shop"])


def refresh(html, opener=fetch, ping=alive, quiet=False):
    """The page with every card's picture brought up to date, and a report."""
    moved = []
    for card in cards(html):
        wanted = resolve(card, opener, ping)
        if wanted == card["src"]:
            continue
        old = SRC.search(card["markup"])
        updated = card["markup"][: old.start(1)] + wanted + card["markup"][old.end(1):]
        html = html.replace(card["markup"], updated)
        moved.append((card["name"], card["src"], wanted))
        if not quiet:
            print("  %-14s %s" % (card["name"], "animated" if wanted.endswith(".gif") else "still"))
    return html, moved


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Point each creator card at their current picture.")
    parser.add_argument("--check", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--index", default=INDEX, help="page to edit (default: index.html)")
    parser.add_argument("--quiet", action="store_true", help="print nothing unless something changed")
    args = parser.parse_args()

    with open(args.index, encoding="utf-8", newline="") as handle:
        html = handle.read()

    updated, moved = refresh(html, quiet=args.quiet)

    if not moved:
        if not args.quiet:
            print("every creator card is already current (%d cards)" % len(cards(html)))
        return 0

    if args.check:
        print("%d card(s) would move; nothing written (--check)" % len(moved))
        return 0

    temporary = args.index + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    os.replace(temporary, args.index)

    print("%d card(s) updated in %s" % (len(moved), os.path.basename(args.index)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
