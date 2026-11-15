#!/usr/bin/env python3
"""Tests for tools/update-product-descriptions.py against a local fake editor.

The real creator editor needs a signed-in IMVU session, which cannot be part of a
test, so these drive the script against a loopback server that serves the same
shape of page: a form, a hidden CSRF token and a description textarea. What is
being checked is the part that can be wrong without IMVU — that the form is found,
that every other field survives untouched, that nothing is posted without
--apply, and that a page with no form is reported as a missing session rather
than silently skipped.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "update-product-descriptions.py")

CSRF = "csrf-2f9a7c"
PAGE = """<!doctype html><html><body>
<form action="/creator/edit_product_html/{pid}" method="post" enctype="{enctype}">
  <input type="hidden" name="csrf_token" value="{csrf}">
  <input type="hidden" name="products_id" value="{pid}">
  <input type="text" name="products_name" value="xr noct den">
  <textarea name="products_description">{description}</textarea>
  <input type="submit" name="save" value="Save">
</form></body></html>"""

NO_FORM = """<!doctype html><html><body><h1>IMVU</h1><p>Sign in to continue.</p></body></html>"""


class State(object):
    def __init__(self):
        self.descriptions = {"111": "old one", "222": "old two", "333": "old three"}
        self.posts = []
        self.enctype = "application/x-www-form-urlencoded"
        self.formless = {"999"}


class Handler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args):
        pass

    def _send(self, body, code=200):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _page(self, pid):
        return PAGE.format(pid=pid, enctype=self.state.enctype, csrf=CSRF,
                           description=self.state.descriptions.get(pid, ""))

    def do_GET(self):
        pid = self.path.rstrip("/").rsplit("/", 1)[-1]
        if pid in self.state.formless:
            return self._send(NO_FORM)
        return self._send(self._page(pid))

    def do_POST(self):
        pid = self.path.rstrip("/").rsplit("/", 1)[-1]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        kind = self.headers.get("Content-Type", "")
        if "multipart" in kind:
            boundary = kind.split("boundary=")[1].encode()
            fields = {}
            for part in raw.split(b"--" + boundary):
                if b"Content-Disposition" not in part:
                    continue
                head, _, value = part.partition(b"\r\n\r\n")
                name = head.split(b'name="')[1].split(b'"')[0].decode()
                fields[name] = value.rstrip(b"\r\n-").decode()
        else:
            fields = dict(urllib.parse.parse_qsl(raw.decode("utf-8")))
        self.state.posts.append({"id": pid, "fields": fields})
        if "products_description" in fields:
            self.state.descriptions[pid] = fields["products_description"]
        return self._send(self._page(pid))


def load_module():
    spec = importlib.util.spec_from_file_location("update_descriptions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EditorHarness(unittest.TestCase):
    def setUp(self):
        self.state = State()
        Handler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

        self.module = load_module()
        self.module.EDITOR = self.base + "/creator/edit_product_html/%s"
        self.module.time.sleep = lambda _s: None

        self.tmp = tempfile.mkdtemp()
        self.cookie = os.path.join(self.tmp, "session.cookies")
        with open(self.cookie, "w", encoding="utf-8") as handle:
            handle.write("Cookie: sess=keepme; other=1\n")
        self.backup = os.path.join(self.tmp, "backups")
        self.desc = os.path.join(self.tmp, "new.html")
        with open(self.desc, "w", encoding="utf-8") as handle:
            handle.write("<p>the new description</p>")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_script(self, *extra):
        argv = ["update-product-descriptions.py", "--cookie-file", self.cookie,
                "--backup-dir", self.backup, "--delay", "0"] + list(extra)
        self.module.sys.argv = argv
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.module.main()
        return code, out.getvalue()


class FormReading(EditorHarness):
    def test_reads_the_description_and_every_hidden_field(self):
        url, form, field = self.module.read_product("sess=keepme", "111")
        self.assertTrue(url.endswith("/creator/edit_product_html/111"))
        self.assertEqual(field.name, "products_description")
        self.assertEqual(field.value, "old one")
        names = form.names()
        self.assertIn("csrf_token", names)
        self.assertIn("products_id", names)
        self.assertIn("products_name", names)
        # the submit button is parsed, and dropped again when the form is posted
        self.assertIn("save", names)

    def test_a_page_with_no_form_is_a_missing_session(self):
        with self.assertRaises(self.module.SessionMissing):
            self.module.read_product("sess=keepme", "999")

    def test_a_missing_cookie_file_is_a_missing_session(self):
        with self.assertRaises(self.module.SessionMissing):
            self.module.load_cookie(os.path.join(self.tmp, "nope.cookies"))

    def test_cookie_header_prefix_is_stripped_but_never_printed(self):
        cookie = self.module.load_cookie(self.cookie)
        self.assertEqual(cookie, "sess=keepme; other=1")


class Writing(EditorHarness):
    def test_dry_run_posts_nothing(self):
        code, out = self.run_script("--ids", "111", "222", "--description-file", self.desc)
        self.assertEqual(code, 0)
        self.assertEqual(self.state.posts, [])
        self.assertIn("dry run", out)
        self.assertIn("would save", out)

    def test_apply_replaces_only_the_description(self):
        code, out = self.run_script("--ids", "111", "--description-file", self.desc, "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.state.posts), 1)
        posted = self.state.posts[0]["fields"]
        self.assertEqual(posted["products_description"], "<p>the new description</p>")
        self.assertEqual(posted["csrf_token"], CSRF)
        self.assertEqual(posted["products_id"], "111")
        self.assertEqual(posted["products_name"], "xr noct den")
        self.assertNotIn("save", posted)   # a button is only sent if it was clicked
        self.assertEqual(self.state.descriptions["111"], "<p>the new description</p>")
        self.assertIn("confirmed", out)

    def test_the_previous_description_is_backed_up(self):
        self.run_script("--ids", "222", "--description-file", self.desc, "--apply")
        path = os.path.join(self.backup, "222.html")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old two")

    def test_copy_from_takes_the_reference_description(self):
        code, out = self.run_script("--copy-from", "111", "--ids", "222", "333", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state.descriptions["222"], "old one")
        self.assertEqual(self.state.descriptions["333"], "old one")
        self.assertEqual(self.state.descriptions["111"], "old one")

    def test_an_already_matching_description_is_left_alone(self):
        self.state.descriptions["111"] = "the same text"
        with open(self.desc, "w", encoding="utf-8") as handle:
            handle.write("the same text")
        code, out = self.run_script("--ids", "111", "--description-file", self.desc, "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state.posts, [])
        self.assertIn("already matches", out)

    def test_multipart_forms_are_posted_too(self):
        self.state.enctype = "multipart/form-data"
        code, out = self.run_script("--ids", "111", "--description-file", self.desc, "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state.posts[0]["fields"]["products_description"],
                         "<p>the new description</p>")
        self.assertEqual(self.state.posts[0]["fields"]["csrf_token"], CSRF)

    def test_an_empty_description_is_refused(self):
        open(self.desc, "w", encoding="utf-8").close()
        code, out = self.run_script("--ids", "111", "--description-file", self.desc, "--apply")
        self.assertEqual(code, 1)
        self.assertEqual(self.state.posts, [])
        self.assertIn("refusing", out)


class ProductList(EditorHarness):
    def test_default_ids_come_from_products_json(self):
        path = os.path.join(self.tmp, "products.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump([{"id": 76546193}, {"id": "76508289"}, {}], handle)
        self.assertEqual(self.module.product_ids_from_json(path), ["76546193", "76508289"])
        self.assertEqual(self.module.product_ids_from_json(os.path.join(self.tmp, "absent.json")), [])


class ConsoleEncoding(unittest.TestCase):
    """The shop is named 𝕩𝕣, which a Windows console cannot print by default.

    A crash there would land partway through a batch, with the products already
    saved and the rest untouched — so the console is widened before any output.
    """

    def test_the_shops_characters_print_on_a_narrow_console(self):
        module = load_module()
        raw = io.BytesIO()
        narrow = io.TextIOWrapper(raw, encoding="cp1252")
        real = sys.stdout
        sys.stdout = narrow
        try:
            module.configure_console()
            narrow.write("𝕩𝕣 noct den")
            narrow.flush()
        finally:
            sys.stdout = real

        self.assertEqual(narrow.encoding.lower(), "utf-8")
        self.assertIn("𝕩𝕣 noct den", raw.getvalue().decode("utf-8"))


def load_embed():
    spec = importlib.util.spec_from_file_location(
        "embed_page", os.path.join(ROOT, "tools", "embed-page-in-products.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmbeddingThePage(EditorHarness):
    """The page goes into every product once, and a re-run replaces it in place."""

    def setUp(self):
        EditorHarness.setUp(self)
        self.embed = load_embed()
        self.embed.editor.EDITOR = self.base + "/creator/edit_product_html/%s"
        self.embed.editor.time.sleep = lambda _s: None
        self.ids = ["111", "222", "333"]
        self.original = dict(self.state.descriptions)

    def run_embed(self, *extra):
        self.embed.sys.argv = ["embed-page-in-products.py", "--cookie-file", self.cookie,
                               "--backup-dir", self.backup, "--delay", "0"] + list(extra)
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.embed.main()
        return code, out.getvalue()

    def test_every_product_gets_the_frame_once(self):
        code, out = self.run_embed("--ids", *self.ids, "--apply")

        self.assertEqual(code, 0, out)
        for pid in self.ids:
            text = self.state.descriptions[pid]
            self.assertEqual(text.count(self.embed.START), 1, pid)
            self.assertIn(self.embed.PAGE, text)
            self.assertIn("iframe", text)
            self.assertTrue(text.startswith(self.original[pid]), pid)

    def test_a_second_run_replaces_rather_than_stacks(self):
        self.run_embed("--ids", *self.ids, "--apply")
        code, out = self.run_embed("--ids", *self.ids, "--apply")

        self.assertEqual(code, 0, out)
        for pid in self.ids:
            self.assertEqual(self.state.descriptions[pid].count(self.embed.START), 1, pid)
        self.assertIn("already embeds it", out)

    def test_a_dry_run_posts_nothing(self):
        code, out = self.run_embed("--ids", *self.ids)

        self.assertEqual(code, 0, out)
        self.assertEqual(self.state.posts, [])
        self.assertIn("would save", out)

    def test_the_height_follows_the_flag(self):
        self.run_embed("--ids", "111", "--height", "900", "--apply")

        self.assertIn("height:900px", self.state.descriptions["111"])

    def test_the_copy_goes_in_with_the_frame_where_marked(self):
        code, out = self.run_embed(
            "--ids", *self.ids, "--text", "<b>made to order</b> {{embed}} thanks", "--apply"
        )

        self.assertEqual(code, 0, out)
        text = self.state.descriptions["111"]
        self.assertLess(text.index("made to order"), text.index("<iframe"))
        self.assertLess(text.index("<iframe"), text.index("thanks"))

    def test_copy_without_a_placeholder_puts_the_frame_after_it(self):
        self.run_embed("--ids", "111", "--text", "<i>my copy</i>", "--apply")

        text = self.state.descriptions["111"]
        self.assertLess(text.index("my copy"), text.index("<iframe"))

    def test_a_re_run_with_new_copy_replaces_the_old_copy(self):
        self.run_embed("--ids", "111", "--text", "first version", "--apply")
        code, out = self.run_embed("--ids", "111", "--text", "second version", "--apply")

        self.assertEqual(code, 0, out)
        text = self.state.descriptions["111"]
        self.assertIn("second version", text)
        self.assertNotIn("first version", text)
        self.assertEqual(text.count(self.embed.START), 1)

    def test_a_catalogue_supplies_the_ids(self):
        catalogue = os.path.join(self.tmp, "catalogue.json")
        with open(catalogue, "w", encoding="utf-8") as handle:
            json.dump([{"id": "111"}, {"id": "333"}], handle)

        code, out = self.run_embed("--catalogue", catalogue, "--apply")

        self.assertEqual(code, 0, out)
        self.assertIn("2 product(s) from", out)
        self.assertIn(self.embed.START, self.state.descriptions["111"])
        self.assertNotIn(self.embed.START, self.state.descriptions["222"], "222 was not in the catalogue")

    def test_a_session_that_is_gone_stops_the_run(self):
        self.state.formless = {"111"}

        code, out = self.run_embed("--ids", *self.ids, "--apply")

        self.assertEqual(code, 2, out)
        self.assertEqual(self.state.posts, [])


if __name__ == "__main__":
    unittest.main()
