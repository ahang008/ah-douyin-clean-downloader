"""Offline checks for exact work identity and author isolation in web fallback."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/recover_official_web_detail.py"
SPEC = importlib.util.spec_from_file_location("official_web_detail_recovery_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
VIDEO_ID = "7600000000000000001"
OTHER_ID = "7600000000000000002"
SEC_UID = "MS4wLjFixtureCreator"


class OfficialWebDetailRecoveryTests(unittest.TestCase):
    def response(self, url, item, status=200):
        return SimpleNamespace(url=url, status=status,
                               json=lambda: {"status_code": 0, "aweme_detail": item})

    def test_only_exact_official_detail_and_requested_video_id_are_accepted(self):
        url = "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=" + VIDEO_ID
        item = {"aweme_id": VIDEO_ID, "author": {"sec_uid": SEC_UID}}
        self.assertEqual(MODULE.public_detail_from_response(self.response(url, item), VIDEO_ID), item)
        for wrong in (url.replace("www.douyin.com", "www.douyin.com.evil"),
                      url.replace("https:", "http:"),
                      url.replace("/aweme/detail/", "/aweme/feed/")):
            self.assertIsNone(MODULE.public_detail_from_response(self.response(wrong, item), VIDEO_ID))
        self.assertIsNone(MODULE.public_detail_from_response(self.response(url, item, 404), VIDEO_ID))
        self.assertIsNone(MODULE.public_detail_from_response(self.response(url, {"aweme_id": OTHER_ID}), VIDEO_ID))

    def test_foreign_author_is_rejected_even_with_matching_video_id(self):
        catalog = {"creator": {"sec_uid": SEC_UID}}
        row = {"author": {"sec_uid": SEC_UID, "uid": "123"}}
        self.assertTrue(MODULE.author_matches({"author": {"sec_uid": SEC_UID}}, catalog, row))
        self.assertTrue(MODULE.author_matches({"author": {"uid": "123"}}, catalog, row))
        self.assertFalse(MODULE.author_matches({"author": {"sec_uid": "MS4wLjOther", "uid": "999"}}, catalog, row))
        self.assertFalse(MODULE.author_matches({"author": {"sec_uid": "MS4wLjOther", "uid": "123"}}, catalog, row))
        self.assertFalse(MODULE.author_matches({"author": {"sec_uid": SEC_UID, "uid": "999"}}, catalog, row))
        self.assertFalse(MODULE.author_matches({"author": {"sec_uid": SEC_UID}}, catalog,
                                               {"author": {"sec_uid": "MS4wLjOther"}}))

    def test_only_app_feed_identity_misses_enter_browser_recovery(self):
        manifest = {"videos": {
            VIDEO_ID: {"status": "failed", "last_error": "metadata_id_not_returned: target absent"},
            OTHER_ID: {"status": "failed", "last_error": "curl timeout"},
            "7600000000000000003": {"status": "verified", "last_error": "metadata_id_not_returned"},
        }}
        self.assertEqual(MODULE.eligible_failed_ids(manifest, set(manifest["videos"])), [VIDEO_ID])
        self.assertEqual(MODULE.eligible_failed_ids(manifest, {OTHER_ID}), [])

    def test_browser_response_listener_accepts_playwright_callable_contract(self):
        item = {"aweme_id": VIDEO_ID, "author": {"sec_uid": SEC_UID}}
        response = self.response("https://www.douyin.com/aweme/v1/web/aweme/detail/", item)

        class Page:
            def on(self, event, handler):
                assert event == "response" and hasattr(handler, "__name__")
                self.handler = handler
            def goto(self, *_args, **_kwargs):
                self.handler(response)
            def wait_for_timeout(self, _milliseconds):
                pass
            def close(self):
                pass

        context = SimpleNamespace(new_page=Page)
        self.assertEqual(MODULE.web_detail(context, VIDEO_ID), item)

    def test_media_recovery_rejects_author_directory_symlink_before_any_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            outside = Path(temporary) / "outside"
            (root / "media").mkdir(parents=True)
            outside.mkdir()
            (root / "media" / "Fixture Creator").symlink_to(outside, target_is_directory=True)
            with patch.object(MODULE.backend, "CurlClient", side_effect=AssertionError("network attempted")):
                with self.assertRaisesRegex(RuntimeError, "符号链接"):
                    MODULE.recover_media(root, {}, VIDEO_ID, "Fixture Creator", None)
            self.assertEqual(list(outside.iterdir()), [])

    def test_media_recovery_rejects_media_root_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "media").symlink_to(outside, target_is_directory=True)
            with patch.object(MODULE.backend, "CurlClient", side_effect=AssertionError("network attempted")):
                with self.assertRaisesRegex(RuntimeError, "unsafe_library_subdirectory_symlink:media"):
                    MODULE.recover_media(root, {}, VIDEO_ID, "Fixture Creator", None)
            self.assertEqual(list(outside.iterdir()), [])

    def test_cover_recovery_rejects_covers_directory_symlink_without_external_write(self):
        item = {"video": {"cover": {"url_list": ["https://p16.douyinpic.com/fixture.jpg"]}}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "covers").symlink_to(outside, target_is_directory=True)

            def fake_convert(_source, destination):
                destination.write_bytes(b"external write must be blocked")
                return True

            with patch.object(MODULE.covers, "routes", return_value=[None]), \
                 patch.object(MODULE.covers, "curl_to", return_value=True), \
                 patch.object(MODULE.covers, "convert_to_jpeg", side_effect=fake_convert), \
                 patch.object(MODULE.covers, "valid_jpeg", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "unsafe_library_subdirectory_symlink:covers"):
                    MODULE.recover_cover(root, item, VIDEO_ID, None)
            self.assertEqual(list(outside.iterdir()), [])

    def test_cover_recovery_rejects_cached_image_symlink_without_false_success(self):
        item = {"video": {"cover": {"url_list": ["https://p16.douyinpic.com/fixture.jpg"]}}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            cover_dir = root / "covers"
            cover_dir.mkdir(parents=True)
            outside = Path(temporary) / "outside.jpg"
            outside.write_bytes(b"unchanged outside fixture")
            (cover_dir / (VIDEO_ID + ".jpg")).symlink_to(outside)
            with patch.object(MODULE.covers, "valid_jpeg", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "unsafe_cover_destination_symlink"):
                    MODULE.recover_cover(root, item, VIDEO_ID, None)
            self.assertEqual(outside.read_bytes(), b"unchanged outside fixture")


if __name__ == "__main__":
    unittest.main()
