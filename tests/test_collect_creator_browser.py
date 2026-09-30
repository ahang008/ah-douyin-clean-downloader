"""Offline fixtures only. These tests never import Playwright or use a browser."""
import importlib.util
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_creator_browser.py"
SPEC = importlib.util.spec_from_file_location("collect_creator_browser", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SEC_UID = "MS4wLjUnitFixtureCreator"


def work(number, author=SEC_UID, image=False):
    return {"aweme_id": str(7600000000000000000 + number), "desc": "公开作品 " + str(number),
            "aweme_type": 68 if image else 0, "images": [{"image": True}] if image else None,
            "author": {"sec_uid": author, "uid": "123456", "nickname": "Fixture", "unique_id": "FixtureOnly"},
            "video": {"duration": 1000}, "is_top": number == 1}


def payload(rows, cursor, more, filtered=False):
    return {"status_code": 0, "max_cursor": cursor, "has_more": more, "aweme_list": rows,
            "not_login_module": {"guide_login_tip_exist": filtered}}


class BrowserCollectionFixtures(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "browser-catalog.json"
        self.recorder = MODULE.BrowserCatalog(SEC_UID, self.path)

    def tearDown(self):
        self.directory.cleanup()

    def add(self, cursor, data):
        return self.recorder.add(cursor, data, json.dumps(data).encode())

    def test_real_terminal_and_pinned_dedup_separate_images(self):
        self.add(0, payload([work(1), work(2)], 90, 1))
        self.add(90, payload([work(1), work(3), work(4, image=True)], 0, 0))
        value = self.recorder.save()
        self.assertTrue(value["catalog_complete"])
        self.assertEqual(value["video_count"], 3)
        self.assertEqual(value["non_video_work_count"], 1)
        self.assertEqual([page["requested_cursor"] for page in value["page_evidence"]], [0, 90])
        self.assertEqual(value["page_evidence"][-1]["has_more"], False)

    def test_out_of_order_events_link_only_after_first_page(self):
        self.add(90, payload([work(2)], 0, 0))
        self.assertFalse(self.recorder.save()["catalog_complete"])
        self.assertFalse(self.path.exists())
        self.add(0, payload([work(1)], 90, 1))
        self.assertTrue(self.recorder.save()["catalog_complete"])

    def test_gap_is_not_complete_even_with_large_work_count(self):
        self.add(0, payload([work(number) for number in range(1, 140)], 90, 1))
        self.add(80, payload([work(141)], 0, 0))
        value = self.recorder.catalog()
        self.assertFalse(value["catalog_complete"])
        self.assertEqual(value["unlinked_response_count"], 1)

    def test_filtered_terminal_never_claims_full_then_normal_login_resets(self):
        self.add(0, payload([work(1)], 0, 0, filtered=True))
        self.assertFalse(self.recorder.catalog()["catalog_complete"])
        self.add(0, payload([work(2)], 0, 0))
        value = self.recorder.catalog()
        self.assertTrue(value["catalog_complete"])
        self.assertEqual([row["video_id"] for row in value["videos"]], [work(2)["aweme_id"]])

    def test_wrong_creator_aborts_before_saving_page(self):
        with self.assertRaises(ValueError):
            self.add(0, payload([work(1), work(2, "MS4wLjDifferentCreator")], 0, 0))
        self.assertFalse(self.recorder.pages)
        self.assertFalse(self.path.exists())

    def test_prelogin_errors_do_not_poison_new_valid_first_page(self):
        self.recorder.errors.append("Official authorpost payload failed validation")
        self.add(0, payload([work(1)], 0, 0))
        self.assertTrue(self.recorder.catalog()["catalog_complete"])

    def test_image_only_terminal_does_not_claim_complete_video_catalog(self):
        self.add(0, payload([work(1, image=True)], 0, 0))
        value = self.recorder.catalog()
        self.assertTrue(value["pagination_exhausted"])
        self.assertFalse(value["catalog_complete"])

    def test_missing_list_or_terminal_flag_cannot_be_success(self):
        fixtures = [{"status_code": 0}, {"status_code": 0, "aweme_list": [work(1)], "max_cursor": 0},
                    payload([], 0, 0), payload([work(1)], 0, 1)]
        for data in fixtures:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.add(0, data)
        self.assertFalse(self.recorder.pages)

    def test_payload_projection_discards_tokens_and_hashes_saved_file(self):
        data = payload([work(1)], 0, 0)
        data["UIFID"] = data["msToken"] = "SECRET_SENTINEL_123"
        data["aweme_list"][0]["author"]["session_token"] = "SECRET_SENTINEL_123"
        self.add(0, data)
        value = self.recorder.save()
        import hashlib
        evidence = value["page_evidence"][0]
        file = Path(evidence["file"])
        self.assertEqual(evidence["saved_file_sha256"], hashlib.sha256(file.read_bytes()).hexdigest())
        for saved in Path(self.directory.name).rglob("*.json"):
            self.assertNotIn("SECRET_SENTINEL_123", saved.read_text())
        self.assertNotEqual(evidence["payload_sha256"], evidence["saved_file_sha256"])

    def test_existing_output_retained_exactly_when_new_capture_is_partial(self):
        original = b'{"catalog_complete":true,"fixture":"existing bytes"}\n'
        self.path.write_bytes(original)
        self.recorder = MODULE.BrowserCatalog(SEC_UID, self.path)
        self.add(0, payload([work(1)], 90, 1))
        value = self.recorder.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(Path(value["active_attempt_catalog"]).is_file())
        self.add(90, payload([work(2)], 0, 0))
        self.recorder.save()
        self.assertTrue(json.loads(self.path.read_text())["catalog_complete"])

    def test_exact_official_origin_and_creator_request_only(self):
        suffix = "?sec_user_id=" + SEC_UID + "&max_cursor=90&a_bogus=private-signature"
        self.assertEqual(MODULE.official_request_cursor(MODULE.POST_URL + suffix, SEC_UID), 90)
        for url in [MODULE.POST_URL.replace("https:", "http:") + suffix,
                    MODULE.POST_URL.replace("www.douyin.com", "www.douyin.com.evil") + suffix,
                    MODULE.POST_URL.replace("www.douyin.com", "www.douyin.com:444") + suffix,
                    MODULE.POST_URL + suffix.replace(SEC_UID, "MS4wLjOther")]:
            self.assertIsNone(MODULE.official_request_cursor(url, SEC_UID))

    def test_nonloopback_or_credentialed_cdp_is_rejected_without_process_read(self):
        for url in ["http://example.com:9222", "http://localhost:9222", "http://user:secret@127.0.0.1:9222", "http://127.0.0.1:9222?secret=1"]:
            args = SimpleNamespace(cdp=url, user_data_dir=None, browser_pid=None)
            with self.assertRaises(ValueError):
                MODULE.verify_isolated_browser(args)

    def test_safe_failure_categories_do_not_include_payload_secrets(self):
        data = {"status_code": 0, "msToken": "SECRET_SENTINEL_123"}
        with self.assertRaises(MODULE.CaptureValidationError) as error:
            self.add(0, data)
        self.assertEqual(error.exception.reason, "aweme_list_wrong_shape")
        self.assertEqual(MODULE.safe_response_diagnostics(data),
                         {"status_code": 0, "has_more": None, "aweme_list_count": None})
        self.assertNotIn("SECRET_SENTINEL_123", json.dumps(MODULE.safe_response_diagnostics(data)))

    def test_navigation_classifier_only_allows_known_transients(self):
        self.assertEqual(MODULE.browser_error_kind(RuntimeError("Execution context was destroyed, most likely because of a navigation")), "navigation_transient")
        self.assertEqual(MODULE.browser_error_kind(RuntimeError("Target page, context or browser has been closed")), "target_closed")
        self.assertEqual(MODULE.browser_error_kind(RuntimeError("Unexpected signed_url=PRIVATE")), "other")

    def test_login_marker_alone_is_not_panel_evidence(self):
        class Locator:
            def __init__(self, visible): self.visible = visible
            async def count(self): return 1 if self.visible else 0
            def nth(self, index): return self
            async def is_visible(self): return self.visible

        class Frame:
            def __init__(self, qr): self.qr = qr
            def get_by_text(self, *args, **kwargs): return Locator(True)
            def locator(self, selector): return Locator(self.qr if selector.startswith("canvas") else False)

        for qr, expected in ((False, False), (True, True)):
            frame = Frame(qr)
            page = SimpleNamespace(frames=[frame], main_frame=frame)
            self.assertEqual(asyncio.run(MODULE.login_panel_observed(page)), expected)

    def isolated_arguments(self):
        directory = (Path(self.directory.name) / "dedicated-profile").resolve()
        directory.mkdir(exist_ok=True)
        return SimpleNamespace(cdp="http://127.0.0.1:43123", user_data_dir=directory, browser_pid=4321)

    def process_results(self, args, argv_port="0", listen="127.0.0.1:43123"):
        command = "Edge --user-data-dir=" + str(args.user_data_dir) + " --remote-debugging-port=" + argv_port
        return [SimpleNamespace(returncode=0, stdout=command),
                SimpleNamespace(returncode=0, stdout="p4321\nn" + listen + "\n")]

    def test_random_port_checks_active_file_and_owned_loopback_listener(self):
        args = self.isolated_arguments()
        (args.user_data_dir / "DevToolsActivePort").write_text("43123\n/devtools/browser/private-path\n")
        with patch.object(MODULE.subprocess, "run", side_effect=self.process_results(args)) as calls:
            self.assertEqual(MODULE.verify_isolated_browser(args), 43123)
        self.assertIn("-iTCP:43123", calls.call_args_list[-1].args[0])

    def test_random_port_mismatch_is_rejected_before_listener_lookup(self):
        args = self.isolated_arguments()
        (args.user_data_dir / "DevToolsActivePort").write_text("49999\n/devtools/browser/private-path\n")
        with patch.object(MODULE.subprocess, "run", side_effect=self.process_results(args)) as calls:
            with self.assertRaises(ValueError):
                MODULE.verify_isolated_browser(args)
        self.assertEqual(calls.call_count, 1)

    def test_random_or_explicit_port_public_listener_is_rejected(self):
        args = self.isolated_arguments()
        (args.user_data_dir / "DevToolsActivePort").write_text("43123\n")
        for argv_port in ("0", "43123"):
            for listen in ("*:43123", "[::]:43123", "192.168.1.10:43123"):
                with self.subTest(argv_port=argv_port, listen=listen):
                    with patch.object(MODULE.subprocess, "run", side_effect=self.process_results(args, argv_port, listen)):
                        with self.assertRaises(ValueError):
                            MODULE.verify_isolated_browser(args)

    def test_explicit_port_accepts_owned_ipv6_loopback_listener(self):
        args = self.isolated_arguments()
        with patch.object(MODULE.subprocess, "run", side_effect=self.process_results(args, "43123", "[::1]:43123")):
            self.assertEqual(MODULE.verify_isolated_browser(args), 43123)


if __name__ == "__main__":
    unittest.main()
