#!/usr/bin/env python3
"""Keep every creator card's picture pointing at that creator's current one.

    python tools/refresh-creators.py            # rewrite the cards that moved
    python tools/refresh-creators.py --check     # report, change nothing

Each card shows IMVU's own picture, addressed by the id that also drives its shop
link -- no copy is shipped, so nothing stale sits in the page. The catch is that
the animated avatars are not addressable by id alone: their filenames carry a
token (`avatars/119374898_20348436876a9f21e11da5a.gif`) that only the creator's
own avatar page shows. `catalog/image_avatar.php?cid=<id>` is addressable by id
but always answers with a static 80x110 JPEG, so it is the last resort rather
than the source. This resolves each id back to a picture and rewrites `src` only
where it has actually moved.
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
SRC = re.compile(r'src="([^"]+)"')

# the avatar page carries other members' thumbnails too, so a file is only taken
# when its filename starts with this creator's own id
OWN_FILE = r'https?://[^"\'\s)]*/avatars/%s_([0-9a-f]+)\.(gif|jpg|jpeg|png)'


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
    """The creator's own picture on their avatar page, an animated file first.

    None means the page showed no file of theirs -- some profiles are private,
    and some accounts upload a Snap photo that carries no id.
    """
    found = re.findall(OWN_FILE % re.escape(mid), page, re.IGNORECASE)
    if not found:
        return None
    animated = next((token for token, extension in found if extension.lower() == "gif"), None)
    token, extension = (animated, "gif") if animated else found[0]
    return (
        "https://userimages-akm.imvu.com/catalog/includes/modules/phpbb2/images/"
        "avatars/%s_%s.%s" % (mid, token, extension.lower())
    )


def cards(html):
    """Every card in the row, as the name, shop id, picture and markup it has."""
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

    A file on the creator's own page wins. Failing that, an animation the card
    already has is kept while it still answers: the id endpoint's JPEG is static,
    so falling back to it would trade a working animation for a still picture.
    Only when neither exists does that JPEG take over.
    """
    try:
        page = opener(AVATAR_PAGE.format(name=urllib.parse.quote(card["name"])))
    except Exception:
        page = ""

    found = picture_for(card["shop"], page)
    if found:
        return found

    if card["src"].lower().endswith(".gif") and ping(card["src"]):
        return card["src"]

    return AVATAR_IMAGE.format(mid=card["shop"])


def refresh(html, opener=fetch, ping=alive):
    """The page with every card's picture current, and the cards that moved."""
    moved = []
    for card in cards(html):
        wanted = resolve(card, opener, ping)
        if wanted == card["src"]:
            continue
        old = SRC.search(card["markup"])
        updated = card["markup"][: old.start(1)] + wanted + card["markup"][old.end(1):]
        html = html.replace(card["markup"], updated)
        moved.append((card["name"], wanted))
    return html, moved


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Point each creator card at their current picture.")
    parser.add_argument("--check", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()

    with open(INDEX, encoding="utf-8", newline="") as handle:
        html = handle.read()

    updated, moved = refresh(html)

    if not moved:
        print("every creator card is already current (%d cards)" % len(cards(html)))
        return 0

    for name, wanted in moved:
        print("  %-14s %s" % (name, "animated" if wanted.lower().endswith(".gif") else "still"))

    if args.check:
        print("%d card(s) would move; nothing written (--check)" % len(moved))
        return 0

    temporary = INDEX + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    os.replace(temporary, INDEX)

    print("%d card(s) updated in index.html" % len(moved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
