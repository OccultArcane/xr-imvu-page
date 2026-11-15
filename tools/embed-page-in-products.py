#!/usr/bin/env python3
"""Put the published XR page inside every product page, as an iframe.

IMVU product pages are edited through the same creator editor the description
rewriter drives: read the page's form, keep every field as found, replace only
the description textarea, post it back, then re-read to confirm it stuck. This
script builds an embed block and does that for every product in one pass.

    python tools/embed-page-in-products.py                    # dry run, all products
    python tools/embed-page-in-products.py --apply
    python tools/embed-page-in-products.py --ids 76499972 --apply
    python tools/embed-page-in-products.py --url https://example.com/ --height 720 --apply
    python tools/embed-page-in-products.py --text "Made to order. {{embed}}" --apply
    python tools/embed-page-in-products.py --text-file copy.html --apply

The copy is yours: --text or --text-file is written into every product, and
{{embed}} in it is where the frame goes. Without a placeholder the copy is
written first and the frame follows it. Without either flag the product keeps
whatever text it had and only gains the frame.

The block is marked, so the script is safe to run again: an existing embed is
replaced in place rather than stacked under the old one, and a description with
no embed gets one appended. Anything else in the description is left alone.

Every replaced description is backed up first, exactly as the rewriter does, so a
bad pass can be undone by pointing that script's --description-file at a backup.

Whether IMVU keeps the iframe is not something this script can guarantee: the
editor may sanitise markup on save. That is why each product is re-read and
compared afterwards, and why the block carries a plain link under the frame — if
the iframe is stripped, the link is what a visitor still gets.

Exit codes: 0 everything done, 1 nothing to do or a page could not be parsed,
2 no usable session, 3 a write failed or did not stick.
"""

import argparse
import importlib.util
import os
import sys
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PAGE = "https://occultarcane.github.io/xr-imvu-page/"
START = "<!-- xr-embed:start -->"
END = "<!-- xr-embed:end -->"
DEFAULT_HEIGHT = 640


def load_editor():
    """The description rewriter, imported for its form handling.

    Its file name has dashes in it, so it cannot be imported by name; loading it
    by path keeps one implementation of the editor protocol rather than two.
    """
    spec = importlib.util.spec_from_file_location(
        "xr_product_editor", os.path.join(HERE, "update-product-descriptions.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


editor = load_editor()


def frame(url, height):
    """The frame itself, with a link under it for whatever strips iframes."""
    return "\n".join(
        [
            '<div style="position:relative;width:100%;max-width:960px;margin:0 auto;">',
            '  <iframe src="%s" title="XR — Premium Creator" loading="lazy" '
            'allowfullscreen style="width:100%%;height:%dpx;border:0;'
            'background:#050507;display:block;"></iframe>' % (url, height),
            "</div>",
            '<p style="text-align:center;margin:10px 0 0;">'
            '<a href="%s" target="_blank" rel="noopener">open XR in a new tab</a></p>' % url,
        ]
    )


def block(url, height, text=None):
    """The marked region this script owns: the copy, and the frame.

    {{embed}} in the copy is replaced by the frame, so the frame can sit
    anywhere in it. Without a placeholder the copy comes first and the frame
    follows. Without copy the region is just the frame.
    """
    body = (text or "").strip()
    if "{{embed}}" in body:
        inside = body.replace("{{embed}}", frame(url, height))
    elif body:
        inside = body + "\n\n" + frame(url, height)
    else:
        inside = frame(url, height)
    return "\n".join([START, inside, END])


def with_embed(description, url, height, text=None):
    """The description with exactly one marked region in it.

    A marked region is replaced where it stands — so a re-run with different
    copy replaces the old copy instead of leaving it behind — and a description
    without one has the region appended, never stacked twice.
    """
    fresh = block(url, height, text)
    current = description or ""
    if START in current and END in current:
        head, rest = current.split(START, 1)
        _, tail = rest.split(END, 1)
        return head + fresh + tail
    if not current.strip():
        return fresh
    return current.rstrip() + "\n\n" + fresh


def main():
    parser = argparse.ArgumentParser(
        description="Embed the published XR page in every product description.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default=PAGE, help="page to embed (default: %s)" % PAGE)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                        help="iframe height in px (default: %d)" % DEFAULT_HEIGHT)
    copy = parser.add_mutually_exclusive_group()
    copy.add_argument("--text", metavar="HTML",
                      help="copy to write into every product; {{embed}} marks where the frame goes")
    copy.add_argument("--text-file", metavar="PATH",
                      help="a file whose contents are that copy")
    parser.add_argument("--ids", nargs="+", metavar="ID",
                        help="products to touch (default: every id in assets/products.json)")
    parser.add_argument("--cookie-file", default=editor.DEFAULT_COOKIE,
                        help="file holding the signed-in cookie (never printed)")
    parser.add_argument("--backup-dir", default=editor.DEFAULT_BACKUP,
                        help="where each replaced description is saved first")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds between products (default: 2)")
    parser.add_argument("--apply", action="store_true",
                        help="actually save; without this the run is a dry run")
    parser.add_argument("--quiet", action="store_true", help="only report changes and errors")
    args = parser.parse_args()

    editor.configure_console()

    if not args.url.startswith(("https://", "http://")):
        print("--url must be an http(s) address: %r" % args.url)
        return 1

    text = args.text
    if args.text_file:
        if not os.path.exists(args.text_file):
            print("no such file: %s" % args.text_file)
            return 1
        with open(args.text_file, encoding="utf-8") as handle:
            text = handle.read()

    try:
        cookie = editor.load_cookie(args.cookie_file)
    except editor.SessionMissing as exc:
        print("no session: %s" % exc)
        return 2

    ids = args.ids or editor.product_ids_from_json(os.path.join(ROOT, "assets", "products.json"))
    if not ids:
        print("nothing to do: no product ids given and assets/products.json is empty")
        return 1

    print("embedding: %s" % args.url)
    print("frame: %dpx tall%s" % (args.height, "" if args.apply else "  (dry run — add --apply to save)"))
    if text is not None:
        print("copy: %d chars, " % len(text.strip())
              + ("frame placed at {{embed}}" if "{{embed}}" in text else "frame appended below it"))
    print("products: %d" % len(ids))

    os.makedirs(args.backup_dir, exist_ok=True)
    written = failed = 0

    for index, product_id in enumerate(ids, 1):
        try:
            url, form, field = editor.read_product(cookie, product_id)
        except editor.SessionMissing as exc:
            print("[%d/%d] %s: no session - %s" % (index, len(ids), product_id, exc))
            return 2
        except (editor.ShopUnreadable, urllib.error.URLError, OSError) as exc:
            print("[%d/%d] %s: could not read - %s" % (index, len(ids), product_id, exc))
            failed += 1
            continue

        before = field.value
        target = with_embed(before, args.url, args.height, text)
        unchanged = target.strip() == (before or "").strip()

        if not args.quiet or not unchanged:
            print("[%d/%d] %s  %s" % (index, len(ids), product_id,
                                      "already embeds it" if unchanged else "will embed"))

        if unchanged:
            continue

        with open(os.path.join(args.backup_dir, "%s.html" % product_id), "w", encoding="utf-8") as handle:
            handle.write(before)

        if not args.apply:
            print(editor.line("would save", "yes (dry run)"))
            continue

        action = urllib.parse.urljoin(url, form.action or url)
        body, content_type = editor.encode(form, {field.name: target})
        try:
            page, final = editor.fetch(action, cookie, data=(body, content_type))
            confirmed = editor.pick_form(editor.parse_forms(page))
            saved = confirmed.description_field() if confirmed else None
            if saved is not None and saved.value.strip() == target.strip():
                print(editor.line("saved", "confirmed at %s" % final))
                written += 1
            elif saved is not None:
                print(editor.line("NOT saved", "the editor still shows the old text"))
                failed += 1
            else:
                print(editor.line("saved", "posted, but no editor form came back to confirm"))
                written += 1
        except urllib.error.HTTPError as exc:
            print(editor.line("failed", "HTTP %s from %s" % (exc.code, action)))
            failed += 1
        except (urllib.error.URLError, OSError) as exc:
            print(editor.line("failed", "%s" % exc))
            failed += 1

        if index < len(ids):
            time.sleep(max(0.0, args.delay))

    print("done: %d saved, %d failed, %d product(s) considered%s"
          % (written, failed, len(ids), "" if args.apply else " (dry run)"))
    if failed:
        print("description backups are in %s" % args.backup_dir)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
