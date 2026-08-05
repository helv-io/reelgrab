"""Tests for runtime state persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from reelgrab.state import StateStore


class TestStateStore(unittest.TestCase):
    def test_persist_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runtime_state.yaml"
            store = StateStore(path)
            store.update(auto_download=False, success_caption="hi")
            self.assertTrue(path.is_file())

            store2 = StateStore(path)
            self.assertIs(store2.state.auto_download, False)
            self.assertEqual(store2.state.success_caption, "hi")

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "nope.yaml")
            self.assertIsNone(store.state.auto_download)


if __name__ == "__main__":
    unittest.main()
