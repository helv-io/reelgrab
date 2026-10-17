"""Rules about the Docker publish workflow that a later edit must not undo."""
import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "docker.yml"


class DockerLatestTagTest(unittest.TestCase):
    """A version tag must never move :latest.

    Tag jobs finish out of order, and the last one would own it.
    """

    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.rules = [
            ln.strip() for ln in self.text.splitlines() if "value=latest" in ln or "type=raw" in ln
        ]

    def test_automatic_latest_is_off(self):
        # latest=auto tags :latest for every semver release.
        self.assertRegex(self.text, r"flavor:\s*\|\s*\n\s*latest=false")

    def test_latest_only_from_the_default_branch(self):
        latest = [r for r in self.rules if "value=latest" in r]
        self.assertEqual(len(latest), 1, "exactly one rule may publish :latest")
        self.assertTrue(re.search(r"enable=\{\{is_default_branch\}\}", latest[0]), latest[0])

    def test_no_other_rule_can_publish_latest(self):
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "value=latest" in stripped:
                continue
            self.assertNotIn(":latest", stripped.replace("`:latest`", ""), stripped)


if __name__ == "__main__":
    unittest.main()
