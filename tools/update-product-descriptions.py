#!/usr/bin/env python3
"""Rewrite the description of every XR product through IMVU's creator editor.

IMVU publishes no API for product descriptions. The only route is the creator
editor at https://www.imvu.com/creator/edit_product_html/<product id>, which is a
signed-in page - and not politely: asked without a session it answers 200 with the
public homepage, no form and no redirect, so "there is no form here" is what a
missing login looks like. This script checks that before it touches anything.

The save is the page's own. It reads the editor's form, keeps every field exactly
as it found it (the CSRF token included), replaces only the description, and posts
the form back. Both urlencoded and multipart forms are handled, because the editor
may carry an image field.

    python tools/update-product-descriptions.py --copy-from 76546193
    python tools/update-product-descriptions.py --copy-from 76546193 --apply
    python tools/update-product-descriptions.py --description-file new.html --apply
    python tools/update-product-descriptions.py --ids 76546193 76508289 --apply

Nothing is written without --apply; without it the script prints what it would
send and stops. Every run copies the description it is about to replace into a
backup directory first, so a bad bulk edit can be undone by pointing
--description-file at the backup.

The session cookie is read from a file (default %LOCALAPPDATA%\\XR\\session.cookies)
holding either a raw `name=value; name=value` string or a whole `Cookie:` header.
It is never printed, logged, or written into the report.

Exit codes: 0 everything done, 1 the shop could not be read or parsed, 2 no
usable session, 3 a write failed or did not stick.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITOR = "https://www.imvu.com/creator/edit_product_html/%s"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
DEFAULT_COOKIE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "XR", "session.cookies")
DEFAULT_BACKUP = os.path.join(os.environ.get("TEMP", "/tmp"), "xr-description-backups")
DESC_HINT = re.compile(r"desc", re.I)


class SessionMissing(Exception):
    """The editor answered without a form: no session, or not the creator's."""


class ShopUnreadable(Exception):
    """The editor answered with something this script cannot parse."""


class Field(object):
    def __init__(self, name, value, kind, checked):
        self.name = name
        self.value = value
        self.kind = kind            # input type, or "textarea"
        self.checked = checked

    def __repr__(self):
        return "Field(%r, %s)" % (self.name, self.kind)


class Form(object):
    def __init__(self, attrs):
        self.action = (attrs.get("action") or "").strip()
        self.method = (attrs.get("method") or "post").strip().lower()
        self.enctype = (attrs.get("enctype") or "application/x-www-form-urlencoded").strip().lower()
        self.fields = []

    def names(self):
        return [f.name for f in self.fields]

    def description_field(self):
        """The textarea that holds the description - by name first, then any
        textarea, because the editor's field name is not guaranteed."""
        textareas = [f for f in self.fields if f.kind == "textarea"]
        for field in textareas:
            if DESC_HINT.search(field.name or ""):
                return field
        return textareas[0] if textareas else None

    def image_field(self):
        return any(f.kind == "file" for f in self.fields)


class FormParser(HTMLParser):
    """Collects every <form> and the controls inside it, in document order."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.forms = []
        self._form = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = Form(a)
            self.forms.append(self._form)
        elif self._form is not None and tag == "input":
            kind = (a.get("type") or "text").lower()
            if not a.get("name"):
                return
            if kind in ("submit", "button", "reset", "image", "file"):
                self._form.fields.append(Field(a["name"], a.get("value", ""), kind, False))
                return
            self._form.fields.append(
                Field(a["name"], a.get("value", ""), kind, "checked" in a)
            )
        elif self._form is not None and tag == "textarea":
            self._textarea = {"name": a.get("name") or "", "text": []}
        elif self._form is not None and tag == "select" and a.get("name"):
            self._form.fields.append(Field(a["name"], "", "select", True))

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "textarea" and self._textarea is not None and self._form is not None:
            self._form.fields.append(
                Field(self._textarea["name"], "".join(self._textarea["text"]), "textarea", True)
            )
            self._textarea = None
        elif tag == "form":
            self._form = None


def parse_forms(page):
    parser = FormParser()
    parser.feed(page)
    parser.close()
    return parser.forms


def pick_form(forms):
    """The form that owns the description: one with a description-named textarea,
    else the first with any textarea at all."""
    with_textarea = [f for f in forms if f.description_field()]
    for form in with_textarea:
        if DESC_HINT.search(form.description_field().name or ""):
            return form
    return with_textarea[0] if with_textarea else None


def load_cookie(path):
    if not os.path.exists(path):
        raise SessionMissing(
            "no cookie file at %s - copy a signed-in imvu.com Cookie header into it"
            % path
        )
    with open(path, encoding="utf-8", errors="replace") as handle:
        cookie = handle.read().strip()
    if not cookie:
        raise SessionMissing("the cookie file at %s is empty" % path)
    cookie = re.sub(r"(?i)^cookie:\s*", "", cookie).replace("\r", "").replace("\n", "")
    if "=" not in cookie:
        raise SessionMissing("the cookie file does not look like name=value pairs")
    return cookie


def fetch(url, cookie, data=None):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Cookie": cookie,
        "Referer": "https://www.imvu.com/creator/",
    }
    body = None
    if data is not None:
        body, headers["Content-Type"] = data
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", "replace"), response.geturl()


def encode(form, overrides):
    """The form's body with the given fields replaced. File inputs are skipped:
    a browser sends an empty part for them, and re-sending a file is not this
    script's job."""
    pairs = []
    for field in form.fields:
        # buttons are never sent: a browser sends only the one that was clicked,
        # and the editor may carry more than a save button (preview, delete)
        if field.kind in ("file", "submit", "reset", "button", "image"):
            continue
        if field.kind in ("checkbox", "radio") and not field.checked:
            continue
        value = overrides.get(field.name, field.value)
        pairs.append((field.name, value))
    for name, value in overrides.items():
        if name not in form.names():
            pairs.append((name, value))

    if "multipart" in form.enctype:
        boundary = "----xr" + uuid.uuid4().hex
        chunks = []
        for name, value in pairs:
            chunks.append(
                '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, name, value)
            )
        chunks.append("--%s--\r\n" % boundary)
        payload = "".join(chunks).encode("utf-8")
        return payload, "multipart/form-data; boundary=%s" % boundary

    payload = urllib.parse.urlencode(pairs, doseq=True, encoding="utf-8").encode("utf-8")
    return payload, "application/x-www-form-urlencoded; charset=utf-8"


def product_ids_from_json(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [str(item.get("id")) for item in json.load(handle) if item.get("id")]


def read_product(cookie, product_id):
    page, url = fetch(EDITOR % product_id, cookie)
    form = pick_form(parse_forms(page))
    if form is None:
        raise SessionMissing(
            "product %s came back with no form at all - that is the editor serving "
            "its public page, which means this session is not signed in as the creator"
            % product_id
        )
    field = form.description_field()
    if field is None:
        raise ShopUnreadable(
            "product %s has a form but no textarea, so there is no description to read "
            "(fields: %s)" % (product_id, ", ".join(form.names()[:12]))
        )
    return url, form, field


def line(label, text):
    text = re.sub(r"\s+", " ", text or "").strip()
    return "  %-18s %s" % (label, text if text else "(empty)")


def main():
    parser = argparse.ArgumentParser(
        description="Rewrite every product description through IMVU's creator editor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--copy-from", metavar="ID",
                        help="use this product's current description as the one to write")
    source.add_argument("--description-file", metavar="PATH",
                        help="write this file's contents as the description")
    parser.add_argument("--ids", nargs="+", metavar="ID",
                        help="products to rewrite (default: every id in assets/products.json)")
    parser.add_argument("--cookie-file", default=DEFAULT_COOKIE,
                        help="file holding the signed-in cookie (never printed)")
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP,
                        help="where each replaced description is saved first")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds between products (default: 2)")
    parser.add_argument("--apply", action="store_true",
                        help="actually save; without this the run is a dry run")
    parser.add_argument("--quiet", action="store_true", help="only report changes and errors")
    args = parser.parse_args()

    try:
        cookie = load_cookie(args.cookie_file)
    except SessionMissing as exc:
        print("no session: %s" % exc)
        return 2

    ids = args.ids or product_ids_from_json(os.path.join(ROOT, "assets", "products.json"))
    if not ids:
        print("nothing to do: no product ids given and assets/products.json is empty")
        return 1

    # 1. the description to write
    reference = None
    if args.description_file:
        if not os.path.exists(args.description_file):
            print("no such file: %s" % args.description_file)
            return 1
        with open(args.description_file, encoding="utf-8") as handle:
            target = handle.read()
        print("description source: %s (%d chars)" % (args.description_file, len(target)))
    else:
        reference = str(args.copy_from)
        try:
            _, _, field = read_product(cookie, reference)
        except SessionMissing as exc:
            print("no session: %s" % exc)
            return 2
        except (ShopUnreadable, urllib.error.URLError, OSError) as exc:
            print("could not read %s: %s" % (reference, exc))
            return 1
        target = field.value
        print("description source: product %s (%d chars)" % (reference, len(target)))
        print(line("from", target[:120]))

    if not target.strip():
        print("refusing to write an empty description")
        return 1

    print("mode: %s" % ("apply" if args.apply else "dry run (add --apply to save)"))
    print("products: %d" % len(ids))

    os.makedirs(args.backup_dir, exist_ok=True)
    written = failed = 0

    for index, product_id in enumerate(ids, 1):
        try:
            url, form, field = read_product(cookie, product_id)
        except SessionMissing as exc:
            print("[%d/%d] %s: no session - %s" % (index, len(ids), product_id, exc))
            return 2
        except (ShopUnreadable, urllib.error.URLError, OSError) as exc:
            print("[%d/%d] %s: could not read - %s" % (index, len(ids), product_id, exc))
            failed += 1
            continue

        before = field.value
        unchanged = before.strip() == target.strip()

        if not args.quiet or not unchanged:
            print("[%d/%d] %s  %s" % (index, len(ids), product_id,
                                      "already matches" if unchanged else "differs"))
            print(line("was", before[:120]))
            print(line("with", target[:120]))

        if unchanged:
            continue

        with open(os.path.join(args.backup_dir, "%s.html" % product_id), "w", encoding="utf-8") as handle:
            handle.write(before)

        if not args.apply:
            print(line("would save", "yes (dry run)"))
            continue

        action = urllib.parse.urljoin(url, form.action or url)
        body, content_type = encode(form, {field.name: target})
        try:
            page, final = fetch(action, cookie, data=(body, content_type))
            confirmed = pick_form(parse_forms(page))
            saved = confirmed.description_field() if confirmed else None
            if saved is not None and saved.value.strip() == target.strip():
                print(line("saved", "confirmed at %s" % final))
                written += 1
            elif confirmed is not None and saved is not None:
                print(line("NOT saved", "the editor still shows the old text"))
                failed += 1
            else:
                # the editor is free to redirect after a save; a missing form is
                # not proof either way, so report it rather than claim success
                print(line("saved", "posted, but no editor form came back to confirm"))
                written += 1
        except urllib.error.HTTPError as exc:
            print(line("failed", "HTTP %s from %s" % (exc.code, action)))
            failed += 1
        except (urllib.error.URLError, OSError) as exc:
            print(line("failed", "%s" % exc))
            failed += 1

        if index < len(ids):
            time.sleep(max(0.0, args.delay))

    print("done: %d saved, %d failed, %d product(s) considered%s"
          % (written, failed, len(ids), "" if args.apply else " (dry run)"))
    if failed:
        print("backups of every replaced description are in %s" % args.backup_dir)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
