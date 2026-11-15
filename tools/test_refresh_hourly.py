#!/usr/bin/env python3
"""Tests for tools/refresh-hourly.py, the one action the hourly task runs.

The wrapper's job is small and entirely about what happens when something goes
wrong: the creators refresh must still run after the products refresh fails, a
failure must be visible in the log rather than silent, and the exit code must say
so. Every test builds its own `tools/` directory with stand-in scripts, so none
of this touches IMVU or the real page.
"""

import importlib.util
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "refresh-hourly.py")


def load():
    spec = importlib.util.spec_from_file_location("refresh_hourly", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TheWrapper(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.workspace = tempfile.mkdtemp(prefix="xr-hourly-")
        self.tools = os.path.join(self.workspace, "tools")
        os.makedirs(self.tools)
        self.log = os.path.join(self.workspace, "xr-hourly.log")
        # redirect both the log and the tools this wrapper looks for
        self.module.LOG_PATH = self.log
        self.module.ROOT = self.workspace

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def script(self, name, body):
        with open(os.path.join(self.tools, name), "w", encoding="utf-8") as handle:
            handle.write(body)

    def lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def test_a_successful_tool_is_logged_with_its_last_line(self):
        self.script("refresh-products.py", "print('wrote products.json with 8 products')\n")

        ok = self.module.run("products", "refresh-products.py")

        self.assertTrue(ok)
        self.assertIn("products: ok in ", self.lines()[-1], self.lines())
        self.assertIn("wrote products.json with 8 products", self.lines()[-1])

    def test_a_failure_is_logged_with_its_exit_code_and_complaint(self):
        self.script("refresh-creators.py", "import sys\nprint('could not read the shop', file=sys.stderr)\nsys.exit(1)\n")

        ok = self.module.run("creators", "refresh-creators.py")

        self.assertFalse(ok)
        logged = self.lines()[-1]
        self.assertIn("creators: failed in ", logged)
        self.assertIn("(exit 1)", logged)
        self.assertIn("could not read the shop", logged)

    def test_a_missing_tool_is_a_failure_not_a_crash(self):
        ok = self.module.run("creators", "not-here.py")

        self.assertFalse(ok)
        self.assertIn("is missing", self.lines()[-1])

    def test_a_failure_in_the_first_tool_does_not_stop_the_second(self):
        self.script("refresh-products.py", "import sys\nsys.exit(1)\n")
        self.script("refresh-creators.py", "print('every creator card is already current')\n")
        self.module.TOOLS = (("products", "refresh-products.py"), ("creators", "refresh-creators.py"))

        code = self.module.main()

        self.assertEqual(code, 1)
        logged = "\n".join(self.lines())
        self.assertIn("products: failed", logged)
        self.assertIn("creators: ok", logged)

    def test_the_exit_code_is_zero_only_when_both_ran(self):
        self.script("refresh-products.py", "print('wrote products.json')\n")
        self.script("refresh-creators.py", "print('every creator card is already current')\n")
        self.module.TOOLS = (("products", "refresh-products.py"), ("creators", "refresh-creators.py"))

        self.assertEqual(self.module.main(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
