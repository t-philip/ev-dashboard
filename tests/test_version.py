import re
import unittest
from pathlib import Path

import evdash


class Version(unittest.TestCase):
    def test_code_and_package_metadata_agree(self):
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"', text, re.M)
        self.assertIsNotNone(match, "pyproject.toml declares no version")
        self.assertEqual(evdash.__version__, match.group(1))

    def test_version_looks_like_a_version(self):
        self.assertRegex(evdash.__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
