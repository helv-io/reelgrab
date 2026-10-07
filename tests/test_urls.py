"""Unit tests for short-form URL detection."""

from __future__ import annotations

import unittest

from reelgrab.urls import (
    DEFAULT_URL_PATTERNS,
    canonicalize_url,
    extract_urls,
    find_matching_urls,
    is_amplify_video_url,
    is_http_url,
    is_matching_url,
)

AMPLIFY = (
    "https://video.twimg.com/amplify_video/2102222769186537472/"
    "vid/avc1/3840x2160/lweKF1l9KuqH6_Jl.mp4?tag=29"
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

    def test_instagram_post_and_tv(self) -> None:
        self.assertEqual(len(find_matching_urls("https://instagram.com/p/XYZ/", PATTERNS)), 1)
        self.assertEqual(
            len(find_matching_urls("https://www.instagram.com/tv/XYZ/", PATTERNS)), 1
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

    def test_threads_bluesky_reddit_and_twitter_status(self) -> None:
        samples = [
            "https://x.com/someuser/status/1234567890",
            "https://twitter.com/someuser/status/1234567890",
            "https://www.threads.net/@someuser/post/ABC123",
            "https://bsky.app/profile/user.example.com/post/3abc",
            "https://v.redd.it/abc123",
            "https://www.reddit.com/r/videos/comments/abc123/title/",
            "https://redd.it/abc123",
        ]
        for sample in samples:
            found = find_matching_urls(sample, PATTERNS)
            self.assertEqual(found, [sample], sample)

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

    def test_is_http_url_accepts_http_https(self) -> None:
        self.assertTrue(is_http_url("https://instagram.com/reel/ABC/"))
        self.assertTrue(is_http_url("http://example.com/x"))
        self.assertFalse(is_http_url(""))
        self.assertFalse(is_http_url("ftp://example.com/x"))
        self.assertFalse(is_http_url("file:///tmp/x"))
        self.assertFalse(is_http_url("javascript:alert(1)"))
        self.assertFalse(is_http_url("https://"))
        self.assertFalse(is_http_url("not a url"))
        # Embedded credentials obscure the host — reject.
        self.assertFalse(is_http_url("https://user:pass@example.com/reel/ABC"))

    def test_extract_urls_skips_non_http(self) -> None:
        text = "see ftp://files.example.com/a.mp4 and https://www.instagram.com/reel/ABC/"
        urls = extract_urls(text)
        self.assertEqual(urls, ["https://www.instagram.com/reel/ABC/"])

    def test_amplify_video_example_and_variants(self) -> None:
        found = find_matching_urls(f"look {AMPLIFY}", PATTERNS)
        self.assertEqual(found, [AMPLIFY])
        self.assertTrue(is_amplify_video_url(AMPLIFY))
        no_query = AMPLIFY.split("?", 1)[0]
        self.assertTrue(is_amplify_video_url(no_query))
        older = (
            "http://video.twimg.com/amplify_video/1499/vid/720x720/abcDEF.mp4"
        )
        self.assertTrue(is_amplify_video_url(older))
        self.assertTrue(is_amplify_video_url(AMPLIFY.replace("video.", "VIDEO.")))
        # Trailing punctuation in chat is stripped before matching.
        self.assertEqual(find_matching_urls(AMPLIFY + ".", PATTERNS), [AMPLIFY])

    def test_amplify_video_rejects_close_but_wrong_shapes(self) -> None:
        self.assertFalse(
            is_amplify_video_url(
                "https://video.twimg.com/amplify_video/1/pl/playlist.m3u8"
            )
        )
        self.assertFalse(
            is_amplify_video_url(
                "https://video.twimg.com/ext_tw_video/1/pu/vid/avc1/320x180/a.mp4"
            )
        )
        self.assertFalse(
            is_amplify_video_url("https://twitter.com/user/status/2102222769186537472")
        )
        status = "https://x.com/someuser/status/1234567890123456789"
        self.assertEqual(find_matching_urls(status, PATTERNS), [status])
        # Short hosts must not match lookalikes such as box.com.
        self.assertEqual(
            find_matching_urls("https://box.com/user/status/1", PATTERNS),
            [],
        )

    def test_amplify_dedupes_tag_query(self) -> None:
        bare = AMPLIFY.split("?", 1)[0]
        text = f"{AMPLIFY} {bare}?tag=14"
        self.assertEqual(len(find_matching_urls(text, PATTERNS)), 1)

    def test_canonicalize_non_http_scheme_normalized(self) -> None:
        # Dedupe keys should still be stable even for odd input.
        self.assertTrue(canonicalize_url("https://WWW.Example.com/Path/").startswith("https://"))


if __name__ == "__main__":
    unittest.main()
