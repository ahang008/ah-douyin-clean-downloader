"""Offline listener ownership fixtures; no socket, browser, or network calls."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "launch_browser_collector.py"
SPEC = importlib.util.spec_from_file_location("launch_browser_collector_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LauncherPortFixtures(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.profile = Path(self.directory.name).resolve()
        self.process = SimpleNamespace(pid=4321, poll=Mock(return_value=None))
        self.port = 43123

    def tearDown(self):
        self.directory.cleanup()

    def test_preselected_owned_loopback_port_needs_no_active_port_file(self):
        listeners = SimpleNamespace(returncode=0, stdout="p4321\nn127.0.0.1:43123\nn[::1]:43123\n")
        with patch.object(MODULE.time, "monotonic", side_effect=[0, 0]), \
             patch.object(MODULE.time, "sleep") as sleep, \
             patch.object(MODULE.subprocess, "run", return_value=listeners) as cli, \
             patch.object(MODULE.socket, "socket", side_effect=AssertionError("Unexpected real socket")), \
             patch.object(Path, "read_text", side_effect=AssertionError("Preselected port must not require DevToolsActivePort")):
            self.assertEqual(MODULE.wait_for_port(self.process, self.profile, expected_port=self.port), self.port)
        command = cli.call_args.args[0]
        self.assertIn("-a", command)
        self.assertEqual(command[command.index("-p") + 1], "4321")
        self.assertIn("-iTCP:43123", command)
        sleep.assert_not_called()
        self.assertFalse((self.profile / "DevToolsActivePort").exists())

    def test_other_pid_listener_does_not_make_owned_port_ready_and_times_out(self):
        # Model lsof's -a/-p filter: another process already owning this port
        # produces no listener records for our launcher-owned PID.
        listener_table = [{"pid": 9999, "address": "127.0.0.1:43123"}]

        def fake_lsof(command, **kwargs):
            self.assertEqual(command[0], "lsof")
            self.assertIn("-a", command)
            requested_pid = int(command[command.index("-p") + 1])
            self.assertEqual(requested_pid, self.process.pid)
            selected = [entry for entry in listener_table if entry["pid"] == requested_pid]
            return SimpleNamespace(returncode=0 if selected else 1,
                                   stdout="".join("p" + str(entry["pid"]) + "\nn" + entry["address"] + "\n" for entry in selected))

        with patch.object(MODULE.time, "monotonic", side_effect=[0, 0, 0.2, 0.4, 0.6]), \
             patch.object(MODULE.time, "sleep") as sleep, \
             patch.object(MODULE.subprocess, "run", side_effect=fake_lsof) as cli, \
             patch.object(MODULE.socket, "socket", side_effect=AssertionError("Unexpected real socket")), \
             patch.object(Path, "read_text", side_effect=AssertionError("Unexpected port-file dependency")):
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                MODULE.wait_for_port(self.process, self.profile, timeout=0.5, expected_port=self.port)
        self.assertEqual(cli.call_count, 3)
        self.assertEqual(sleep.call_count, 3)

    def test_owned_wildcard_or_public_listener_is_rejected_even_with_loopback(self):
        for address in ("*:43123", "[::]:43123", "192.168.1.10:43123"):
            with self.subTest(address=address):
                listeners = SimpleNamespace(returncode=0, stdout="p4321\nn127.0.0.1:43123\nn" + address + "\n")
                with patch.object(MODULE.time, "monotonic", side_effect=[0, 0]), \
                     patch.object(MODULE.time, "sleep") as sleep, \
                     patch.object(MODULE.subprocess, "run", return_value=listeners), \
                     patch.object(MODULE.socket, "socket", side_effect=AssertionError("Unexpected real socket")):
                    with self.assertRaisesRegex(RuntimeError, "non-loopback"):
                        MODULE.wait_for_port(self.process, self.profile, expected_port=self.port)
                sleep.assert_not_called()


class BrowserDNSModeFixtures(unittest.TestCase):
    def test_off_and_always_do_not_probe_normal_dns(self):
        with patch.object(MODULE.socket, "getaddrinfo", side_effect=AssertionError("Unexpected DNS probe")):
            self.assertFalse(MODULE.needs_dns_tunnel("off"))
            self.assertTrue(MODULE.needs_dns_tunnel("always"))

    def test_auto_keeps_normal_public_dns_direct(self):
        with patch.object(MODULE.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("1.2.3.4", 443))]):
            self.assertFalse(MODULE.needs_dns_tunnel("auto"))

    def test_auto_repairs_fake_or_failed_official_dns(self):
        with patch.object(MODULE.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("198.18.1.65", 443))]):
            self.assertTrue(MODULE.needs_dns_tunnel("auto"))
        with patch.object(MODULE.socket, "getaddrinfo", side_effect=MODULE.socket.gaierror("fixture")):
            self.assertTrue(MODULE.needs_dns_tunnel("auto"))


if __name__ == "__main__":
    unittest.main()
