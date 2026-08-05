"""Unit tests for short-form URL detection."""

from __future__ import annotations

import unittest

from reelgrab.urls import (
    DEFAULT_URL_PATTERNS,
    canonicalize_url,
    extract_urls,
    find_matching_urls,
    is_matching_url,
)

PATTERNS = list(DEFAULT_URL_PATTERNS)


class TestUrls(unittest.TestCase):
    def test_extract_urls_strips_punctuation(self) -> None:
        text = "see https://www.instagram.com/reel/ABC123/ please."
        urls = extract_urls(text)
        self.assertEqual(urls, ["https://www.instagram.com/reel/ABC123/"])

    def test_find_instagram_reel(self) -> None:
        text = "mom sent https://www.instagram.com/reel/CyQ7uxjOUpM/?igsh=abc"
        found = find_matching_urls(text, PATTERNS)
        self.assertEqual(len(found), 1)
        self.assertIn("CyQ7uxjOUpM", found[0])

    def test_ignore_instagram_long_post_and_tv(self) -> None:
        # Short-form only: classic /p/ and /tv/ are not matched by defaults
        self.assertEqual(find_matching_urls("https://instagram.com/p/XYZ/", PATTERNS), [])
        self.assertEqual(
            find_matching_urls("https://www.instagram.com/tv/XYZ/", PATTERNS), []
        )

    def test_youtube_shorts_only(self) -> None:
        shorts = find_matching_urls(
            "https://www.youtube.com/shorts/dQw4w9WgXcQ", PATTERNS
        )
        self.assertEqual(len(shorts), 1)
        full = find_matching_urls(
            "https://youtube.com/watch?v=dQw4w9WgXcQ", PATTERNS
        )
        self.assertEqual(full, [])

    def test_facebook_reel(self) -> None:
        self.assertTrue(
            find_matching_urls("https://www.facebook.com/reel/1234567890/", PATTERNS)
        )
        self.assertTrue(find_matching_urls("https://fb.watch/AbCdEfG/", PATTERNS))
        self.assertTrue(
            find_matching_urls(
                "https://www.facebook.com/share/r/1AbCdEfG/", PATTERNS
            )
        )

    def test_tiktok(self) -> None:
        self.assertTrue(
            find_matching_urls(
                "https://www.tiktok.com/@user/video/7123456789012345678", PATTERNS
            )
        )
        self.assertTrue(find_matching_urls("https://vm.tiktok.com/ZMabcdef/", PATTERNS))
        self.assertTrue(find_matching_urls("https://vt.tiktok.com/ZSxyz/", PATTERNS))

    def test_ignore_unrelated(self) -> None:
        text = "https://example.com/video/123"
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

    def test_custom_pattern_override(self) -> None:
        patterns = [r"example\.com/clip/"]
        urls = find_matching_urls(
            "watch https://example.com/clip/123", patterns
        )
        self.assertEqual(len(urls), 1)


if __name__ == "__main__":
    unittest.main()
