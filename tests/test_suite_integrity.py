"""The suite checking on itself.

"A test suite that has never executed is not coverage" is one of the rules this
project is built to, and it was learned from a real system whose fifteen
reactor tests had never run because the launcher was a shim whose module was
never installed. A suite that silently stops collecting looks exactly like a
suite that passes.
"""

from __future__ import annotations

import unittest
from pathlib import Path

MINIMUM_TESTS = 150


class SuiteIntegrityTests(unittest.TestCase):
    def test_the_suite_actually_collects_tests(self) -> None:
        here = Path(__file__).resolve().parent
        loader = unittest.TestLoader()
        suite = loader.discover(start_dir=str(here), top_level_dir=str(here.parent))
        self.assertEqual(loader.errors, [], f"a test module failed to import: {loader.errors}")
        self.assertGreaterEqual(
            suite.countTestCases(), MINIMUM_TESTS,
            "the suite collected far fewer tests than expected; something stopped being discovered",
        )

    def test_every_bothy_module_is_importable(self) -> None:
        """Catches a syntax error or a circular import that only bites at runtime."""
        import importlib

        package = Path(__file__).resolve().parent.parent / "bothy"
        names = sorted(p.stem for p in package.glob("*.py") if p.stem != "__init__")
        self.assertGreater(len(names), 15, "modules are being found")
        for name in names:
            with self.subTest(module=name):
                importlib.import_module(f"bothy.{name}")


if __name__ == "__main__":
    unittest.main()
