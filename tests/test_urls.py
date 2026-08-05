"""Unit tests for URL detection."""

from __future__ import annotations

import unittest

from reelgrab.urls import (
    canonicalize_url,
    extract_urls,
    find_matching_urls,
    is_matching_url,
)

PATTERNS = [
    r"instagram\.com/reel/",
    r"instagram\.com/reels/",
    r"instagram\.com/p/",
    r"instagram\.com/tv/",
    r"instagr\.am/",
    r"l\.instagram\.com/",
]


class TestUrls(unittest.TestCase):
    def test_extract_urls_strips_punctuation(self) -> None:
        text = "see https://www.instagram.com/reel/ABC123/ please."
        urls = extract_urls(text)
        self.assertEqual(urls, ["https://www.instagram.com/reel/ABC123/"])

    def test_find_reel(self) -> None:
        text = "mom sent https://www.instagram.com/reel/CyQ7uxjOUpM/?igsh=abc"
        found = find_matching_urls(text, PATTERNS)
        self.assertEqual(len(found), 1)
        self.assertIn("CyQ7uxjOUpM", found[0])

    def test_find_p_and_tv(self) -> None:
        self.assertTrue(find_matching_urls("https://instagram.com/p/XYZ/", PATTERNS))
        self.assertTrue(
            find_matching_urls("https://www.instagram.com/tv/XYZ/", PATTERNS)
        )

    def test_ignore_non_matching(self) -> None:
        text = "https://youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(find_matching_urls(text, PATTERNS), [])

    def test_html_escaped(self) -> None:
        text = "link: https://www.instagram.com/reel/ABC123/?igsh=x&amp;utm=1"
        self.assertEqual(len(find_matching_urls(text, PATTERNS)), 1)

    def test_canonicalize_strips_query(self) -> None:
        u = "https://www.instagram.com/reel/ABC/?igsh=1"
        self.assertEqual(canonicalize_url(u), "https://instagram.com/reel/ABC")

    def test_dedupe_same_reel_different_query(self) -> None:
        text = (
            "https://www.instagram.com/reel/ABC/?igsh=1 "
            "https://instagram.com/reel/ABC/?utm=2"
        )
        self.assertEqual(len(find_matching_urls(text, PATTERNS)), 1)

    def test_instagr_am(self) -> None:
        self.assertTrue(is_matching_url("https://instagr.am/p/ABC/", PATTERNS))

    def test_l_instagram(self) -> None:
        self.assertTrue(
            is_matching_url(
                "https://l.instagram.com/?u=https%3A%2F%2Fwww.instagram.com%2Freel%2FABC",
                PATTERNS,
            )
        )

    def test_custom_pattern_tiktok_style(self) -> None:
        patterns = [r"tiktok\.com/"]
        urls = find_matching_urls(
            "watch https://www.tiktok.com/@user/video/123", patterns
        )
        self.assertEqual(len(urls), 1)


if __name__ == "__main__":
    unittest.main()
