"""Offline fixtures: no public network, browser, Keychain or real TLS endpoint."""
import importlib.util
import json
from pathlib import Path
import socket
import threading
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "browser_dns_proxy.py"
SPEC = importlib.util.spec_from_file_location("browser_dns_proxy_fixture", SCRIPT)
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


def response_headers(client):
    value = b""
    while b"\r\n\r\n" not in value:
        part = client.recv(4096)
        if not part:
            raise AssertionError("proxy did not complete its response")
        value += part
    head, tail = value.split(b"\r\n\r\n", 1)
    return head, tail


class EchoEndpoint:
    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.client = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.client, _ = self.listener.accept()
                break
            except socket.timeout:
                continue
            except OSError:
                return
        if self.client is None:
            return
        try:
            while not self.stop_event.is_set():
                value = self.client.recv(65536)
                if not value:
                    break
                self.client.sendall(value)
        except OSError:
            pass
        finally:
            self.client.close()

    def close(self):
        self.stop_event.set()
        for value in (self.listener, self.client):
            if value is not None:
                try:
                    value.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                value.close()
        self.thread.join(1.0)


class ScopeTests(unittest.TestCase):
    def test_observed_login_sdk_hosts_are_exact_matches(self):
        observed = (
            "lf-ucenter-web.yhgfb-cn-static.com", "lf-headquarters-speed.yhgfb-cn-static.com",
            "lf-rc1.yhgfb-cn-static.com", "lf-rc2.yhgfb-cn-static.com",
            "lf3-static.bytednsdoc.com", "lf-c-flwb.bytetos.com", "lf3-pendah.bytetos.com",
            "lf-cdn-tos.bytescm.com",
        )
        self.assertEqual(PROXY.DEFAULT_ALLOWED_HOSTS, observed)
        for hostname in observed:
            with self.subTest(hostname=hostname):
                self.assertTrue(PROXY.hostname_allowed(hostname))
                self.assertTrue(PROXY.hostname_allowed(hostname.upper() + "."))
                self.assertFalse(PROXY.hostname_allowed("evil." + hostname))
                self.assertFalse(PROXY.hostname_allowed(hostname + ".evil.test"))
                self.assertFalse(PROXY.hostname_allowed("other." + hostname.split(".", 1)[1]))
                self.assertFalse(PROXY.hostname_allowed(hostname, (), ()))
        for hostname in ("mcs.zijieapi.com", "mon.zijieapi.com", "tnc3-alisc1.zijieapi.com"):
            self.assertFalse(PROXY.hostname_allowed(hostname))

    def test_exact_suffix_scope_not_substring_matching(self):
        for hostname in ("www.douyin.com", "v.douyin.com", "WWW.DOUYIN.COM.", "ttwid.bytedance.com", "v3.douyinvod.com",
                         "lf-douyin-pc-web.douyinstatic.com", "lf-douyin-pc-web-back.douyinstatic.com"):
            self.assertTrue(PROXY.hostname_allowed(hostname), hostname)
        for hostname in ("douyin.com.evil.test", "evil-douyin.com", "127.0.0.1", "localhost", "www.douyin.com@evil.test", "www..douyin.com", "www.douyin.com/path",
                         "mon.zijieapi.com", "tnc3-alisc1.zijieapi.com"):
            self.assertFalse(PROXY.hostname_allowed(hostname), hostname)

    def test_all_non_public_ipv4_classes_are_rejected(self):
        rejected = ("10.0.0.1", "172.16.1.1", "192.168.1.1", "127.0.0.1", "169.254.1.1",
                    "198.18.0.1", "198.19.255.254", "100.64.1.1", "192.0.2.1", "198.51.100.1",
                    "203.0.113.1", "224.0.0.1", "240.0.0.1", "0.0.0.0", "::1", "not-an-ip")
        for address in rejected:
            with self.subTest(address=address), self.assertRaises(PROXY.DNSAddressRejected):
                PROXY.public_ipv4(address)
        self.assertEqual(PROXY.public_ipv4("8.8.8.8"), "8.8.8.8")


class DoHTests(unittest.TestCase):
    def test_fixture_cache_and_entire_mixed_answer_rejection(self):
        calls = []
        def fetch(hostname, bootstrap):
            calls.append((hostname, bootstrap))
            return json.dumps({"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8", "TTL": 60}]}).encode()
        resolver = PROXY.PublicDoHResolver(json_fetcher=fetch)
        self.assertEqual(resolver("WWW.DOUYIN.COM."), ["8.8.8.8"])
        self.assertEqual(resolver("www.douyin.com"), ["8.8.8.8"])
        self.assertEqual(calls, [("www.douyin.com", "1.1.1.1")])
        bad = PROXY.PublicDoHResolver(json_fetcher=lambda *_: json.dumps({"Status": 0, "Answer": [
            {"type": 1, "data": "8.8.8.8"}, {"type": 1, "data": "198.18.1.1"}]}).encode())
        with self.assertRaises(PROXY.DNSAddressRejected):
            bad("www.douyin.com")

    def test_curl_pins_bootstrap_and_retains_certificate_verification(self):
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (json.dumps({"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8"}]}).encode(), None)
        with mock.patch.object(PROXY.subprocess, "Popen", return_value=process) as spawn, \
                mock.patch.object(PROXY.shutil, "which", return_value="/usr/bin/curl"):
            resolver = PROXY.PublicDoHResolver()
            self.assertEqual(resolver("www.douyin.com"), ["8.8.8.8"])
            command = spawn.call_args.args[0]
            self.assertEqual(command[command.index("--resolve") + 1], "cloudflare-dns.com:443:1.1.1.1")
            self.assertNotIn("--insecure", command)
            self.assertNotIn("-k", command)
            self.assertIn("--noproxy", command)
            self.assertEqual(spawn.call_args.kwargs["stderr"], PROXY.subprocess.DEVNULL)


class ConnectTests(unittest.TestCase):
    def test_scope_and_port_fail_before_dns(self):
        resolver = mock.Mock(return_value=["8.8.8.8"])
        proxy = PROXY.BrowserDNSProxy(resolver=resolver)
        port = proxy.start()
        try:
            for line, code in ((b"CONNECT www.douyin.com:80 HTTP/1.1", b"403"),
                               (b"CONNECT douyin.com.evil.test:443 HTTP/1.1", b"403"),
                               (b"GET https://www.douyin.com/private?secret=synthetic HTTP/1.1", b"405"),
                               (b"CONNECT [::1]:443 HTTP/1.1", b"400")):
                with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                    client.sendall(line + b"\r\nCookie: synthetic-cookie\r\n\r\n")
                    header, _ = response_headers(client)
                    self.assertIn(code, header.split(b"\r\n", 1)[0])
            resolver.assert_not_called()
            events = proxy.stats()
            self.assertTrue(events)
            self.assertTrue(all(set(value) == {"hostname", "status"} for value in events))
            self.assertNotIn("synthetic", json.dumps(events))
            self.assertNotIn("Cookie", json.dumps(events))
        finally:
            proxy.stop()
        self.assertEqual(proxy.owned_socket_count, 0)

    def test_fake_dns_is_rejected_before_connector(self):
        connector = mock.Mock()
        proxy = PROXY.BrowserDNSProxy(resolver=lambda _: ["198.18.0.3"], connector=connector)
        port = proxy.start()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                client.sendall(b"CONNECT www.douyin.com:443 HTTP/1.1\r\n\r\n")
                header, _ = response_headers(client)
                self.assertIn(b"502", header)
            connector.assert_not_called()
            self.assertIn({"hostname": "www.douyin.com", "status": "dns_address_rejected"}, proxy.events)
        finally:
            proxy.stop()

    def test_opaque_bytes_coalesced_after_connect_are_preserved(self):
        echo = EchoEndpoint()
        calls, logs = [], []
        def connector(address, port, timeout):
            calls.append((address, port))
            # Test-only redirection after the proxy's public-address guard.
            return socket.create_connection(("127.0.0.1", echo.port), timeout=timeout)
        proxy = PROXY.BrowserDNSProxy(resolver=lambda _: ["8.8.8.8"], connector=connector, logger=logs.append)
        port = proxy.start()
        payload = b"\x16\x03\x03\x00\x05TLS-opaque-cookie-synthetic" + bytes(range(256)) * 512
        try:
            self.assertEqual(proxy._listener.getsockname()[0], "127.0.0.1")
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                request = b"CONNECT www.douyin.com:443 HTTP/1.1\r\nProxy-Authorization: synthetic-secret\r\n\r\n"
                client.sendall(request + payload[:100])
                header, received = response_headers(client)
                self.assertIn(b"200 Connection Established", header)
                client.sendall(payload[100:])
                client.shutdown(socket.SHUT_WR)
                while True:
                    part = client.recv(65536)
                    if not part:
                        break
                    received += part
                self.assertEqual(received, payload)
            self.assertEqual(calls, [("8.8.8.8", 443)])
            self.assertTrue(all(set(value) == {"hostname", "status"} for value in logs))
            self.assertNotIn("synthetic", json.dumps(logs))
            self.assertNotIn("Authorization", json.dumps(logs))
        finally:
            proxy.stop()
            echo.close()
        self.assertEqual(proxy.owned_socket_count, 0)

    def test_stop_closes_active_tunnel_and_accept_socket(self):
        echo = EchoEndpoint()
        proxy = PROXY.BrowserDNSProxy(resolver=lambda _: ["8.8.8.8"],
              connector=lambda *_: socket.create_connection(("127.0.0.1", echo.port), timeout=2))
        port = proxy.start()
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            client.sendall(b"CONNECT www.douyin.com:443 HTTP/1.1\r\n\r\n")
            header, _ = response_headers(client)
            self.assertIn(b"200", header)
            self.assertGreaterEqual(proxy.owned_socket_count, 3)
            proxy.stop()
            self.assertEqual(proxy.owned_socket_count, 0)
            self.assertIsNone(proxy.port)
            try:
                self.assertEqual(client.recv(1), b"")
            except ConnectionResetError:
                pass
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.5)
        finally:
            client.close()
            proxy.stop()
            echo.close()


if __name__ == "__main__":
    unittest.main()
