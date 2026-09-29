"""Loopback-only CONNECT proxy for the dedicated, normally secured Edge process.

HTTPS traffic is an opaque TCP stream: no TLS termination, request inspection,
certificate changes, Cookie extraction, or request recording. Public DoH A
records replace fake target DNS; the browser retains normal TLS verification.

Launcher API::

    proxy = BrowserDNSProxy()
    port = proxy.start()            # 127.0.0.1, operating-system chosen port
    try:
        ...  # pass --proxy-server=http://127.0.0.1:<port> to this browser only
    finally:
        proxy.stop()

``proxy.events`` and ``proxy.stats()`` return only hostname/status dictionaries.
Unknown hostnames are denied. Additional observed SDK hosts use ``allowed_hosts``
for exact matching; their parent domains are not implicitly granted. Never
replace the scoped allowlists with a wildcard proxy.
"""
from __future__ import annotations

from collections import deque
import ipaddress
import json
import re
import select
import shutil
import socket
import subprocess
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urlencode


DEFAULT_ALLOWED_SUFFIXES = (
    "douyin.com", "iesdouyin.com", "bytedance.com",
    # douyinstatic.com: observed lf-douyin-pc-web and -back SDK requests
    # from the official creator homepage during the isolated browser probe.
    "douyinpic.com", "douyinvod.com", "douyinstatic.com", "byteimg.com", "bytegoofy.com",
    "pstatp.com", "snssdk.com",
)
# Exact hosts observed serving required SDK assets in the official login UI.
# Their parent domains are intentionally not added to the suffix allowlist.
DEFAULT_ALLOWED_HOSTS = (
    "lf-ucenter-web.yhgfb-cn-static.com",
    "lf-headquarters-speed.yhgfb-cn-static.com",
    "lf-rc1.yhgfb-cn-static.com",
    "lf-rc2.yhgfb-cn-static.com",
    "lf3-static.bytednsdoc.com",
    "lf-c-flwb.bytetos.com",
    "lf3-pendah.bytetos.com",
    "lf-cdn-tos.bytescm.com",
)
DOH_HOST = "cloudflare-dns.com"
DOH_BOOTSTRAP_IPV4 = ("1.1.1.1", "1.0.0.1")
MAX_CONNECT_HEADER_BYTES = 16 * 1024
SPECIAL_IPV4_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
))


class DNSLookupError(RuntimeError):
    """Contains a fixed failure category, never a command or HTTP response."""


class DNSAddressRejected(DNSLookupError):
    pass


class ProxyRequestRejected(ValueError):
    def __init__(self, code: int, category: str, hostname: str | None = None):
        super().__init__(category)
        self.code, self.category, self.hostname = code, category, hostname


def normalize_hostname(value: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError("invalid_hostname")
    hostname = value.lower()
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if len(hostname) > 253 or "." not in hostname:
        raise ValueError("invalid_hostname")
    labels = hostname.split(".")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("invalid_hostname")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    raise ValueError("literal_ip_not_allowed")


def hostname_allowed(hostname: str, suffixes: Iterable[str] = DEFAULT_ALLOWED_SUFFIXES,
                     exact_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS) -> bool:
    try:
        hostname = normalize_hostname(hostname)
    except ValueError:
        return False
    return hostname in exact_hosts or any(hostname == suffix or hostname.endswith("." + suffix) for suffix in suffixes)


def public_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise DNSAddressRejected("invalid_ipv4") from None
    if (not isinstance(address, ipaddress.IPv4Address) or not address.is_global
            or address.is_private or address.is_reserved or address.is_loopback
            or address.is_link_local or address.is_multicast or address.is_unspecified
            or any(address in network for network in SPECIAL_IPV4_NETWORKS)):
        raise DNSAddressRejected("non_public_ipv4")
    return str(address)


def validated_addresses(values: Iterable[str]) -> list[str]:
    # Reject the complete answer if it includes a private/reserved address.
    result = list(dict.fromkeys(public_ipv4(value) for value in values))
    if not result:
        raise DNSLookupError("no_ipv4_answer")
    return result


class PublicDoHResolver:
    """Authenticated Cloudflare JSON DoH, with pinned public bootstrap IPv4.

    Curl is already a prerequisite of the existing downloader. It is used for
    its normal certificate verification and --resolve feature, not as a TLS
    interceptor. Its response is kept in memory and stderr is discarded.
    ``json_fetcher`` is solely dependency injection for offline fixture tests.
    """

    def __init__(self, *, timeout: float = 8.0,
                 json_fetcher: Callable[[str, str], bytes] | None = None):
        self.timeout = timeout
        self._fetcher = json_fetcher
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._processes: set[subprocess.Popen] = set()

    def reopen(self) -> None:
        self._closed.clear()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            try:
                process.kill()
            except OSError:
                pass

    def _fetch(self, hostname: str, bootstrap: str) -> bytes:
        if self._fetcher:
            return self._fetcher(hostname, bootstrap)
        curl = shutil.which("curl")
        if not curl:
            raise DNSLookupError("curl_unavailable")
        query = "https://" + DOH_HOST + "/dns-query?" + urlencode({"name": hostname, "type": "A"})
        command = [curl, "--silent", "--fail", "--noproxy", "*", "--proto", "=https",
                   "--connect-timeout", str(min(4.0, self.timeout)), "--max-time", str(self.timeout),
                   "--resolve", f"{DOH_HOST}:443:{bootstrap}",
                   "-H", "accept: application/dns-json", query]
        with self._lock:
            if self._closed.is_set():
                raise DNSLookupError("resolver_stopped")
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._processes.add(process)
        try:
            try:
                output, _ = process.communicate(timeout=self.timeout + 1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise DNSLookupError("doh_timeout") from None
            if process.returncode or len(output) > 65536:
                raise DNSLookupError("doh_failed")
            return output
        finally:
            with self._lock:
                self._processes.discard(process)

    def __call__(self, hostname: str) -> list[str]:
        hostname = normalize_hostname(hostname)
        with self._lock:
            host_lock = self._host_locks.setdefault(hostname, threading.Lock())
        with host_lock:
            if self._closed.is_set():
                raise DNSLookupError("resolver_stopped")
            with self._lock:
                cached = self._cache.get(hostname)
            if cached and cached[0] > time.monotonic():
                return list(cached[1])
            for bootstrap in DOH_BOOTSTRAP_IPV4:
                try:
                    payload = json.loads(self._fetch(hostname, bootstrap))
                    if payload.get("Status") != 0:
                        raise DNSLookupError("dns_response_failed")
                    answers = [entry for entry in payload.get("Answer", []) if entry.get("type") == 1]
                    addresses = validated_addresses(entry["data"] for entry in answers)
                    ttl = min((int(entry.get("TTL", 60)) for entry in answers), default=60)
                    with self._lock:
                        self._cache[hostname] = (time.monotonic() + max(1, min(ttl, 600)), addresses)
                    return list(addresses)
                except DNSAddressRejected:
                    raise
                except (DNSLookupError, ValueError, TypeError, KeyError, OSError):
                    if self._closed.is_set():
                        raise DNSLookupError("resolver_stopped") from None
            raise DNSLookupError("doh_failed")


class BrowserDNSProxy:
    def __init__(self, *, allowed_suffixes: Iterable[str] = DEFAULT_ALLOWED_SUFFIXES,
                 allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
                 resolver: Callable[[str], Iterable[str]] | None = None,
                 connector: Callable[[str, int, float], socket.socket] | None = None,
                 logger: Callable[[dict], None] | None = None,
                 connect_timeout: float = 8.0, header_timeout: float = 10.0,
                 idle_timeout: float = 120.0, max_connections: int = 64):
        self.allowed_suffixes = tuple(normalize_hostname(value) for value in allowed_suffixes)
        self.allowed_hosts = tuple(normalize_hostname(value) for value in allowed_hosts)
        self._owns_resolver = resolver is None
        self._resolver = PublicDoHResolver() if resolver is None else resolver
        self._connector, self._logger = connector, logger
        self.connect_timeout, self.header_timeout, self.idle_timeout = connect_timeout, header_timeout, idle_timeout
        self._capacity = threading.BoundedSemaphore(max_connections)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._sockets: set[socket.socket] = set()
        self._events: deque[dict] = deque(maxlen=2048)
        self.port: int | None = None

    @property
    def events(self) -> list[dict]:
        with self._lock:
            return [dict(value) for value in self._events]

    def stats(self) -> list[dict]:
        return self.events

    @property
    def owned_socket_count(self) -> int:
        with self._lock:
            return len(self._sockets)

    def _record(self, hostname: str | None, status: str) -> None:
        event = {"hostname": hostname, "status": status}
        with self._lock:
            self._events.append(event)
        if self._logger:
            self._logger(dict(event))

    @staticmethod
    def _close_socket(value: socket.socket) -> None:
        try:
            value.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        value.close()

    def _track(self, value: socket.socket) -> bool:
        with self._lock:
            if self._stop.is_set():
                self._close_socket(value)
                return False
            self._sockets.add(value)
            return True

    def _drop(self, value: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(value)
        self._close_socket(value)

    def start(self) -> int:
        with self._lock:
            if self._listener is not None:
                return self.port
            if any(worker.is_alive() for worker in self._workers):
                raise RuntimeError("previous_proxy_workers_still_stopping")
            self._stop.clear()
            if self._owns_resolver:
                self._resolver.reopen()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen(64)
                listener.settimeout(0.25)
            except BaseException:
                listener.close()
                raise
            self._listener = listener
            self._sockets.add(listener)
            self.port = listener.getsockname()[1]
            self._accept_thread = threading.Thread(target=self._accept, args=(listener,), daemon=True, name="douyin-proxy-accept")
            self._accept_thread.start()
            return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._owns_resolver:
            self._resolver.close()
        with self._lock:
            sockets = list(self._sockets)
            self._listener = None
            workers = list(self._workers)
            accept_thread = self._accept_thread
            self.port = None
        for value in sockets:
            self._drop(value)
        deadline = time.monotonic() + 3.0
        for thread in [accept_thread, *workers]:
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))

    def _accept(self, listener: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._track(client):
                break
            if not self._capacity.acquire(blocking=False):
                self._respond(client, 503)
                self._record(None, "connection_capacity_reached")
                self._drop(client)
                continue
            worker = threading.Thread(target=self._handle, args=(client,), daemon=True, name="douyin-proxy-tunnel")
            with self._lock:
                self._workers.add(worker)
            worker.start()

    @staticmethod
    def _respond(client: socket.socket, code: int) -> None:
        descriptions = {200: "Connection Established", 400: "Bad Request", 403: "Forbidden",
                        405: "Method Not Allowed", 431: "Request Header Fields Too Large",
                        502: "Bad Gateway", 503: "Service Unavailable"}
        response = f"HTTP/1.1 {code} {descriptions[code]}\r\n"
        response += "\r\n" if code == 200 else "Content-Length: 0\r\nConnection: close\r\n\r\n"
        try:
            client.sendall(response.encode("ascii"))
        except OSError:
            pass

    def _read_connect(self, client: socket.socket) -> tuple[str, bytes]:
        client.settimeout(self.header_timeout)
        buffer = bytearray()
        while True:
            data = client.recv(4096)
            if not data:
                raise ProxyRequestRejected(400, "incomplete_connect")
            buffer.extend(data)
            marker = buffer.find(b"\r\n\r\n")
            if marker >= 0:
                if marker + 4 > MAX_CONNECT_HEADER_BYTES:
                    raise ProxyRequestRejected(431, "connect_header_too_large")
                break
            if len(buffer) > MAX_CONNECT_HEADER_BYTES:
                raise ProxyRequestRejected(431, "connect_header_too_large")
        # Only the CONNECT request line is interpreted. Other headers and any
        # coalesced TLS bytes remain opaque and are never passed to the logger.
        try:
            line = bytes(buffer[:marker]).split(b"\r\n", 1)[0].decode("ascii")
            parts = line.split(" ")
            if len(parts) != 3 or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ValueError()
            if parts[0] != "CONNECT":
                raise ProxyRequestRejected(405, "connect_required")
            authority = parts[1]
            if authority.count(":") != 1:
                raise ValueError()
            raw_host, raw_port = authority.split(":")
            hostname = normalize_hostname(raw_host)
            if raw_port != "443":
                raise ProxyRequestRejected(403, "port_not_allowed", hostname)
        except (UnicodeError, ValueError) as exc:
            if isinstance(exc, ProxyRequestRejected):
                raise
            raise ProxyRequestRejected(400, "invalid_connect") from None
        if not hostname_allowed(hostname, self.allowed_suffixes, self.allowed_hosts):
            raise ProxyRequestRejected(403, "hostname_not_allowed", hostname)
        return hostname, bytes(buffer[marker + 4:])

    def _connect(self, addresses: list[str]) -> socket.socket:
        for address in addresses:
            if self._stop.is_set():
                break
            upstream = None
            try:
                if self._connector:
                    upstream = self._connector(address, 443, self.connect_timeout)
                    if not self._track(upstream):
                        break
                else:
                    # The socket is tracked before blocking connect, so stop()
                    # also closes an in-flight connection, not just open tunnels.
                    upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    if not self._track(upstream):
                        break
                    upstream.settimeout(self.connect_timeout)
                    upstream.connect((address, 443))
                return upstream
            except OSError:
                if upstream is not None:
                    self._drop(upstream)
        raise OSError("public_target_connection_failed")

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        client.settimeout(1.0)
        upstream.settimeout(1.0)
        readers = [client, upstream]
        peer = {client: upstream, upstream: client}
        last_activity = time.monotonic()
        while readers and not self._stop.is_set():
            ready, _, _ = select.select(readers, [], [], 0.25)
            if not ready:
                if time.monotonic() - last_activity > self.idle_timeout:
                    return
                continue
            for source in ready:
                data = source.recv(65536)
                if not data:
                    readers.remove(source)
                    try:
                        peer[source].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                peer[source].sendall(data)
                last_activity = time.monotonic()

    def _handle(self, client: socket.socket) -> None:
        hostname, upstream, tunnel_started = None, None, False
        try:
            hostname, pending_bytes = self._read_connect(client)
            addresses = validated_addresses(self._resolver(hostname))
            upstream = self._connect(addresses)
            self._respond(client, 200)
            tunnel_started = True
            self._record(hostname, "tunnel_open")
            if pending_bytes:
                upstream.sendall(pending_bytes)
            self._relay(client, upstream)
            self._record(hostname, "tunnel_closed")
        except ProxyRequestRejected as exc:
            self._respond(client, exc.code)
            self._record(exc.hostname, exc.category)
        except DNSAddressRejected:
            self._respond(client, 502)
            self._record(hostname, "dns_address_rejected")
        except DNSLookupError:
            self._respond(client, 502)
            self._record(hostname, "doh_failed")
        except (OSError, ValueError, TypeError):
            if not tunnel_started:
                self._respond(client, 502)
            self._record(hostname, "stopped" if self._stop.is_set() else "tunnel_io_failed")
        finally:
            if upstream is not None:
                self._drop(upstream)
            self._drop(client)
            self._capacity.release()
            with self._lock:
                self._workers.discard(threading.current_thread())


_default_lock = threading.Lock()
_default_proxy: BrowserDNSProxy | None = None


def start(**kwargs) -> int:
    global _default_proxy
    with _default_lock:
        if _default_proxy is None:
            _default_proxy = BrowserDNSProxy(**kwargs)
        return _default_proxy.start()


def stop() -> None:
    global _default_proxy
    with _default_lock:
        if _default_proxy is not None:
            _default_proxy.stop()
            _default_proxy = None
