"""Captions, thumbnail dimensions, and Matrix media content."""

from __future__ import annotations

import unittest

from reelgrab.captions import build_caption, media_filename, thumbnail_dimensions
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
        self.assertEqual(media_filename("weird id/../x"), "weird_id_x.mp4")


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
