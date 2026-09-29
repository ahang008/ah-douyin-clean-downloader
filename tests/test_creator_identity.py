"""Public URL identity fixtures; no network, browser state, or signature imports."""
import builtins
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_creator.py"
SPEC = importlib.util.spec_from_file_location("public_creator_identity_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SEC_UID = "MS4wLjPublicIdentityFixture"


class CreatorIdentityTests(unittest.TestCase):
    def test_raw_id_and_official_profile_resolve_without_request(self):
        client = SimpleNamespace(fetch=Mock(side_effect=AssertionError("Unexpected request")))
        for value in (SEC_UID, "https://www.douyin.com/user/" + SEC_UID,
                      "https://www.iesdouyin.com/user/" + SEC_UID + "/"):
            self.assertEqual(MODULE.resolve_sec_uid(value, client), SEC_UID)
        client.fetch.assert_not_called()

    def test_share_text_follows_only_official_relative_redirects(self):
        client = SimpleNamespace(fetch=Mock(side_effect=[
            b"HTTP/2 302\r\nLocation: /share/user/?sec_uid=" + SEC_UID.encode() + b"\r\n\r\n"
        ]))
        self.assertEqual(MODULE.resolve_sec_uid("复制打开抖音 https://v.douyin.com/fixture/ 2pm", client), SEC_UID)
        self.assertEqual(client.fetch.call_args.args[0], "https://v.douyin.com/fixture/")

    def test_external_insecure_or_credential_redirect_never_fetched(self):
        for redirect in ("https://attacker.example/user/" + SEC_UID,
                         "http://www.douyin.com/user/" + SEC_UID,
                         "https://user:secret@www.douyin.com/user/" + SEC_UID,
                         "https://www.douyin.com:8443/user/" + SEC_UID):
            with self.subTest(redirect=redirect):
                client = SimpleNamespace(fetch=Mock(return_value=("HTTP/2 302\r\nLocation: " + redirect + "\r\n\r\n").encode()))
                with self.assertRaises(ValueError):
                    MODULE.resolve_sec_uid("https://v.douyin.com/fixture/", client)
                self.assertEqual(client.fetch.call_count, 1)

    def test_no_identity_or_redirect_cycle_is_bounded(self):
        client = SimpleNamespace(fetch=Mock(return_value=b"HTTP/2 200\r\n\r\n<html>fixture</html>"))
        with self.assertRaises(RuntimeError):
            MODULE.resolve_sec_uid("https://v.douyin.com/fixture/", client)
        client = SimpleNamespace(fetch=Mock(return_value=b"HTTP/2 302\r\nLocation: /fixture/\r\n\r\n"))
        with self.assertRaisesRegex(RuntimeError, "Too many"):
            MODULE.resolve_sec_uid("https://v.douyin.com/fixture/", client)
        self.assertEqual(client.fetch.call_count, 8)

    def test_helper_import_needs_no_signer_or_credential_reader(self):
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name.split(".")[0] in ("edge_session", "gmssl", "f2_abogus", "dtk", "Cryptodome"):
                raise AssertionError("Unexpected credential/signature dependency")
            return original(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=guarded):
            spec = importlib.util.spec_from_file_location("identity_dependency_fixture", SCRIPT)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.resolve_sec_uid(SEC_UID, None), SEC_UID)

    def test_normal_fetch_has_no_cookie_state_or_signed_url_in_argv(self):
        client = MODULE.HttpClient(dns_mode="off")
        response = SimpleNamespace(returncode=0, stdout=b"HTTP/2 302\r\n\r\n")
        with patch.object(MODULE.subprocess, "run", return_value=response) as run:
            client.fetch("https://v.douyin.com/fixture/?sec_uid=" + SEC_UID, include_headers=True)
        arguments = run.call_args.args[0]
        self.assertNotIn("--location", arguments)
        self.assertNotIn(SEC_UID, " ".join(arguments))
        self.assertNotIn("cookie", run.call_args.kwargs["input"].decode().lower())
        self.assertFalse(hasattr(client, "session_cookies"))

    def test_fake_dns_doh_bootstrap_is_pinned_and_keeps_tls_verification(self):
        client = MODULE.HttpClient(dns_mode="always")
        response = SimpleNamespace(returncode=0, stdout=b'{"Answer":[{"type":1,"data":"8.8.8.8"}]}')
        with patch.object(MODULE.subprocess, "run", return_value=response) as run:
            self.assertEqual(client.addresses("v.douyin.com"), ["8.8.8.8"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--resolve") + 1], "cloudflare-dns.com:443:1.1.1.1")
        self.assertNotIn("--insecure", command)
        self.assertNotIn("-k", command)
        self.assertEqual(command[command.index("--noproxy") + 1], "cloudflare-dns.com")


if __name__ == "__main__":
    unittest.main()
