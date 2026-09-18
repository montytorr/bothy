"""Checks on the repository itself, not on its behaviour.

These exist because each one has already caught something. They are cheap and
they run with everything else, which is the only reason they get run at all.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(
    path for path in ROOT.rglob("*.py")
    if "vendor" not in path.parts and "__pycache__" not in path.parts
)
DOCS = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").glob("*.md"))

# Scripts that are never intended here. Latin, punctuation and symbols are
# deliberate throughout -- em dashes, middle dots, arrows -- so only alphabets
# that could not be meant are rejected.
#
# Built from code points rather than written as literals, because writing the
# range endpoints out would put the very characters this rejects into the file
# that rejects them. The first version did exactly that and failed against
# itself, which is a good sign about the check and a bad one about the author.
_UNINTENDED = (
    (0x0400, 0x04FF),   # Cyrillic
    (0x0370, 0x03FF),   # Greek
    (0x4E00, 0x9FFF),   # CJK
    (0x3040, 0x30FF),   # kana
    (0x0600, 0x06FF),   # Arabic
)
UNINTENDED_SCRIPTS = re.compile(
    "[" + "".join(f"\\u{low:04x}-\\u{high:04x}" for low, high in _UNINTENDED) + "]"
)


class TextTests(unittest.TestCase):
    def test_no_unintended_scripts_in_source_or_docs(self) -> None:
        """Written after a Russian word appeared in a docstring. Twice.

        A stray word from another alphabet is invisible when skim-reading and
        embarrassing in a public repository, and no amount of care has proved
        sufficient on its own.
        """
        offenders: list[str] = []
        for path in SOURCES + DOCS:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                found = UNINTENDED_SCRIPTS.findall(line)
                if found:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {''.join(found)!r}")
        self.assertEqual(offenders, [], "unintended script found")

    def test_no_personal_paths_in_documentation(self) -> None:
        """A sample path should be a sample, not somebody's home directory."""
        offenders = [
            f"{path.relative_to(ROOT)}"
            for path in DOCS
            if re.search(r"/home/(?!you\b)[a-z][a-z0-9_-]+/", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_no_credential_shaped_strings(self) -> None:
        """Prefixes in comments are fine; a full token never is."""
        pattern = re.compile(r"(sk_live_|gho_|ghp_|xoxb-|xapp-)[A-Za-z0-9_-]{12,}")
        offenders: list[str] = []
        for path in SOURCES + DOCS:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [])

    def test_every_documentation_link_resolves(self) -> None:
        """A README pointing at a file that does not exist is how docs/DESIGN.md
        went missing for a week while the package docstring referenced it."""
        broken: list[str] = []
        for path in DOCS:
            for target in re.findall(r"\]\((docs/[^)]+|[A-Z]+\.md)\)", path.read_text(encoding="utf-8")):
                if not (ROOT / target).exists():
                    broken.append(f"{path.relative_to(ROOT)} -> {target}")
        self.assertEqual(broken, [])

    def test_the_package_docstring_names_every_wake_source(self) -> None:
        """It undersold itself for several commits, naming three of four."""
        text = (ROOT / "bothy" / "__init__.py").read_text(encoding="utf-8").lower()
        for source in ("webhook", "cron", "chat", "polling"):
            self.assertIn(source, text, f"{source} is missing from the package docstring")


if __name__ == "__main__":
    unittest.main()
