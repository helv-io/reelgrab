"""Captions, thumbnail dimensions, and Matrix media content."""

from __future__ import annotations

import unittest

from reelgrab.captions import (
    BRIDGE_CAPTION_LIMIT,
    build_caption,
    caption_html,
    media_filename,
    thumbnail_dimensions,
)
from reelgrab.matrix_content import build_video_content, relates_to
from reelgrab.messages import is_edit, is_historical, strip_reply_fallback


class TestThumbnails(unittest.TestCase):
    def test_vertical_uses_width_cap_not_long_edge(self) -> None:
        # 1080x1920 scaled by width to 640 → 640x1138, not 360x640.
        self.assertEqual(thumbnail_dimensions(1080, 1920), (640, 1138))

    def test_landscape_caps_width(self) -> None:
        self.assertEqual(thumbnail_dimensions(1920, 1080), (640, 360))

    def test_already_small(self) -> None:
        self.assertEqual(thumbnail_dimensions(320, 180), (320, 180))


class TestCaption(unittest.TestCase):
    def test_metadata_caption(self) -> None:
        body = build_caption(
            uploader="clips",
            title="A reel",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
        )
        self.assertEqual(body, "@clips: A reel · https://example.com/reel/abc")

    def test_custom_overrides(self) -> None:
        body = build_caption(
            uploader="clips",
            title="A reel",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            custom="posted",
        )
        self.assertEqual(body, "posted")

    def test_no_metadata_is_filename(self) -> None:
        body = build_caption(
            uploader=None,
            title=None,
            source_url="https://example.com/reel/abc",
            filename="vid.mp4",
        )
        self.assertEqual(body, "vid.mp4")

    def test_filename_from_id(self) -> None:
        self.assertEqual(media_filename("AbC_123"), "AbC_123.mp4")
        self.assertEqual(media_filename("weird id"), "weird_id.mp4")
        self.assertNotIn("/", media_filename("a/b"))

    def test_long_title_still_truncated(self) -> None:
        title = "a" * 200
        body = build_caption(
            uploader="clips",
            title=title,
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
        )
        self.assertEqual(body, f"@clips: {'a' * 179}… · https://example.com/reel/abc")

    def test_description_under_metadata(self) -> None:
        body = build_caption(
            uploader="natgeo",
            title="Fox in snow",
            source_url="https://www.instagram.com/reel/ABC123/",
            filename="ABC123.mp4",
            description="Filmed in Yellowstone last winter.",
        )
        self.assertEqual(
            body,
            "@natgeo: Fox in snow · https://www.instagram.com/reel/ABC123/\n"
            "\n"
            "Filmed in Yellowstone last winter.",
        )

    def test_missing_description_adds_no_blank_line(self) -> None:
        expected = "@clips: A reel · https://example.com/reel/abc"
        for value in (None, "", "   ", "None", "null", "NULL"):
            body = build_caption(
                uploader="clips",
                title="A reel",
                source_url="https://example.com/reel/abc",
                filename="abc.mp4",
                description=value,
            )
            self.assertEqual(body, expected)
            self.assertNotIn("\n", body)
            self.assertNotIn("None", body)

    def test_description_equal_to_title_is_omitted(self) -> None:
        body = build_caption(
            uploader="clips",
            title="Sunset",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description="sunset",
        )
        self.assertEqual(body, "@clips: Sunset · https://example.com/reel/abc")

    def test_description_keeps_only_the_extra_after_the_title(self) -> None:
        body = build_caption(
            uploader="clips",
            title="A fox at dawn",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description="A fox at dawn\n\nFilmed in Yellowstone.",
        )
        self.assertEqual(body.count("A fox at dawn"), 1)
        self.assertIn("@clips: A fox at dawn · https://example.com/reel/abc", body)
        self.assertTrue(body.endswith("Filmed in Yellowstone."))

    def test_long_title_is_not_repeated_when_description_is_the_same_text(self) -> None:
        title = "a" * 200
        body = build_caption(
            uploader="clips",
            title=title,
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description=title,
            max_description_length=500,
        )
        self.assertTrue(body.startswith("@clips · https://example.com/reel/abc\n\n"))
        self.assertEqual(body.count("a" * 200), 1)
        self.assertNotIn("a" * 179 + "…", body)

    def test_description_truncated(self) -> None:
        body = build_caption(
            uploader="clips",
            title="Fox",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description="abcdefghijklmnopqrstuvwxyz",
            max_description_length=10,
        )
        self.assertTrue(body.endswith("abcdefghi…"))

    def test_caption_stays_within_whatsapp_limit(self) -> None:
        body = build_caption(
            uploader="clips",
            title="Title",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description="word " * 500,
            max_description_length=0,
        )
        self.assertLessEqual(len(body), BRIDGE_CAPTION_LIMIT)
        self.assertTrue(body.endswith("…"))
        self.assertIn("@clips: Title", body)

    def test_trailing_hashtag_block_dropped(self) -> None:
        body = build_caption(
            uploader="clips",
            title="Fox",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description="A fox at dawn.\n\n#fox #nature\n#reels",
        )
        self.assertIn("A fox at dawn.", body)
        self.assertNotIn("#fox", body)
        self.assertNotIn("#reels", body)

    def test_inline_hashtags_stay(self) -> None:
        body = build_caption(
            uploader="clips",
            title="Fox",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description="the #1 fox of the day",
        )
        self.assertIn("the #1 fox of the day", body)

    def test_hashtag_only_description_stays(self) -> None:
        body = build_caption(
            uploader="clips",
            title="Fox",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description="#fox #nature",
        )
        self.assertIn("#fox #nature", body)

    def test_include_description_off(self) -> None:
        body = build_caption(
            uploader="clips",
            title="A reel",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            description="Filmed in Yellowstone.",
            include_description=False,
        )
        self.assertEqual(body, "@clips: A reel · https://example.com/reel/abc")

    def test_custom_caption_still_gets_description(self) -> None:
        body = build_caption(
            uploader="clips",
            title="A reel",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            custom="posted",
            description="Filmed in Yellowstone.",
        )
        self.assertEqual(body, "posted\n\nFilmed in Yellowstone.")

    def test_custom_caption_does_not_repeat_itself(self) -> None:
        body = build_caption(
            uploader="clips",
            title="A reel",
            source_url="https://example.com/reel/abc",
            filename="abc.mp4",
            custom="Filmed in Yellowstone.",
            description="Filmed in Yellowstone.",
        )
        self.assertEqual(body, "Filmed in Yellowstone.")

    def test_description_without_metadata_is_a_real_caption(self) -> None:
        body = build_caption(
            uploader=None,
            title=None,
            source_url="https://example.com/reel/abc",
            filename="vid.mp4",
            description="Look at this cat",
        )
        self.assertEqual(body, "https://example.com/reel/abc\n\nLook at this cat")

    def test_caption_html_keeps_line_breaks_and_link(self) -> None:
        plain = (
            "@clips: Fox · https://example.com/reel/abc\n\nLook at this cat <3"
        )
        rendered = caption_html(plain, "https://example.com/reel/abc")
        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertIn("<br>\n", rendered)
        self.assertIn("Look at this cat &lt;3", rendered)
        self.assertIn('<a href="https://example.com/reel/abc">', rendered)
        self.assertNotIn("\n\n", rendered)

    def test_caption_html_one_line_unchanged_shape(self) -> None:
        rendered = caption_html(
            "@clips: Sunset · https://example.com/v",
            "https://example.com/v",
        )
        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertNotIn("<br>", rendered)
        self.assertIn("<a href=", rendered)


class TestMediaContent(unittest.TestCase):
    def test_filename_and_thread(self) -> None:
        content = build_video_content(
            mxc="mxc://example.com/abc",
            body="@clips: A reel · https://example.com/v",
            filename="abc.mp4",
            mime="video/mp4",
            size=1000,
            width=1080,
            height=1920,
            thumbnail_mxc="mxc://example.com/thumb",
            reply_to_event_id="$link",
            thread_root_event_id="$root",
        )
        self.assertEqual(content["filename"], "abc.mp4")
        self.assertNotEqual(content["body"], content["filename"])
        self.assertEqual(content["info"]["thumbnail_info"]["w"], 640)
        self.assertEqual(content["info"]["thumbnail_info"]["h"], 1138)
        self.assertEqual(content["m.relates_to"]["rel_type"], "m.thread")
        self.assertEqual(content["m.relates_to"]["event_id"], "$root")
        self.assertEqual(content["m.relates_to"]["m.in_reply_to"]["event_id"], "$link")
        self.assertFalse(content["m.relates_to"]["is_falling_back"])

    def test_reply_without_thread(self) -> None:
        rel = relates_to(reply_to_event_id="$e")
        self.assertEqual(rel, {"m.in_reply_to": {"event_id": "$e"}})


class TestMessageFilters(unittest.TestCase):
    def test_strip_reply_fallback(self) -> None:
        body = (
            "> <@alice:example.com> https://instagram.com/reel/QUOTED/\n"
            "\n"
            "see https://instagram.com/reel/ACTUAL/"
        )
        self.assertEqual(
            strip_reply_fallback(body),
            "see https://instagram.com/reel/ACTUAL/",
        )

    def test_plain_quote_not_stripped_without_fallback_shape(self) -> None:
        text = "remember:\n> https://instagram.com/reel/ABC/"
        self.assertEqual(strip_reply_fallback(text), text.strip())

    def test_edit_detection(self) -> None:
        self.assertTrue(
            is_edit(
                {
                    "content": {
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$old"},
                        "m.new_content": {"body": "x"},
                    }
                }
            )
        )
        self.assertFalse(is_edit({"content": {"body": "hi"}}))

    def test_historical(self) -> None:
        started = 1_700_000_000_000
        old = {"origin_server_ts": started - 60_000}
        fresh = {"origin_server_ts": started - 1_000}
        self.assertTrue(is_historical(old, started_ms=started))
        self.assertFalse(is_historical(fresh, started_ms=started))
        self.assertFalse(is_historical({}, started_ms=started))


if __name__ == "__main__":
    unittest.main()
