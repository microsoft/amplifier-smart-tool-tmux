"""Whole-tree consumer-brand guard.

This tool is a general-purpose reference implementation; anything
consumer-shaped in its source or tests is a leak. Tokens are built from
fragments so this guard never itself plants a scannable literal, and a
match requires the token NOT be flanked by ASCII letters (catches
``BRAND_ENV`` / ``brand-tool`` while skipping English words that merely
contain a token).
"""

import re
import unittest
from pathlib import Path

_BRAND_TOKENS = ("cor" + "tex", "att" + "end")
_PATTERN = re.compile(
    r"(?<![A-Za-z])(" + "|".join(_BRAND_TOKENS) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


class TestTreeCarriesNoConsumerBrand(unittest.TestCase):
    def test_no_consumer_brand_in_swept_trees(self) -> None:
        base = Path(__file__).resolve().parent.parent
        offenders: list[str] = []
        files: list[Path] = []
        for root in (base / "src", base / "tests"):
            self.assertTrue(root.is_dir(), f"swept tree not found at {root}")
            files.extend(sorted(root.rglob("*.py")))
        for py in files:
            for lineno, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = _PATTERN.search(line)
                if match:
                    offenders.append(
                        f"{py.name}:{lineno}: {match.group(0)!r} in {line.strip()!r}"
                    )
        self.assertEqual(
            offenders,
            [],
            "consumer-brand tokens found in swept trees:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
