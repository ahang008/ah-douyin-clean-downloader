#!/usr/bin/env python3
"""Resolve official creator profile/share-card URLs using public HTTPS redirects.

This lightweight helper uses only the standard library and curl. Catalog pages
are captured by collect_creator_browser.py from the official browser SDK; this
module never reads browser storage, cookies, or Keychain and signs no requests.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import subprocess
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

OFFICIAL_HOSTS = frozenset({"www.douyin.com", "douyin.com", "v.douyin.com",
                            "www.iesdouyin.com", "iesdouyin.com"})
SEC_UID = re.compile(r"MS4wLj[A-Za-z0-9_-]+")
USER_AGENT = "Mozilla/5.0"


def official_url(url: str):
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme == "https" and parsed.hostname in OFFICIAL_HOSTS
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Profile redirects must remain on official HTTPS Douyin domains")
    return parsed


class HttpClient:
    """Fetch public redirect responses without retaining any browser state."""
    def __init__(self, timeout: int = 30, proxy: str | None = None, dns_mode: str = "auto"):
        if timeout <= 0 or dns_mode not in ("auto", "off", "always"):
            raise ValueError("Invalid public URL resolver options")
        self.timeout, self.proxy, self.dns_mode = timeout, proxy, dns_mode
        self.dns = {}

    def needs_repair(self, host: str) -> bool:
        if self.dns_mode == "off" or self.proxy:
            return False
        if self.dns_mode == "always":
            return True
        try:
            addresses = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, 443)]
            return any(address.version == 4 and address in ipaddress.ip_network("198.18.0.0/15")
                       for address in addresses)
        except socket.gaierror:
            return True

    def addresses(self, host: str) -> list[str]:
        if host not in OFFICIAL_HOSTS:
            raise ValueError("Public identity lookup is restricted to official Douyin domains")
        if host not in self.dns:
            query = "https://cloudflare-dns.com/dns-query?" + urlencode({"name": host, "type": "A"})
            addresses = []
            for bootstrap in ("1.1.1.1", "1.0.0.1"):
                result = subprocess.run(["curl", "--silent", "--show-error", "--fail", "--max-time", "15",
                                         "--proto", "=https", "--noproxy", "cloudflare-dns.com",
                                         "--resolve", "cloudflare-dns.com:443:" + bootstrap,
                                         "--header", "accept: application/dns-json", query], capture_output=True)
                if result.returncode != 0:
                    continue
                try:
                    for answer in json.loads(result.stdout).get("Answer", []):
                        if answer.get("type") == 1:
                            address = ipaddress.ip_address(answer["data"])
                            if address.version != 4 or not address.is_global:
                                raise ValueError("Nonpublic DNS answer")
                            addresses.append(str(address))
                except (ValueError, KeyError, TypeError):
                    addresses = []
                if addresses:
                    break
            if not addresses:
                raise RuntimeError("Public DNS lookup failed for the official creator URL")
            self.dns[host] = addresses
        return self.dns[host]

    def fetch(self, url: str, *, include_headers: bool = False) -> bytes:
        parsed = official_url(url)
        candidates = self.addresses(parsed.hostname) if self.needs_repair(parsed.hostname) else [None]
        for address in candidates:
            command = ["curl", "--silent", "--show-error", "--compressed", "--fail-with-body",
                       "--connect-timeout", "8", "--max-time", str(self.timeout), "--proto", "=https"]
            if address:
                command += ["--resolve", f"{parsed.hostname}:443:{address}"]
            if self.proxy:
                command += ["--proxy", self.proxy]
            if include_headers:
                command += ["--dump-header", "-"]
            # Do not export response headers or retain Set-Cookie values.
            config = "url = " + json.dumps(url) + "\nheader = " + json.dumps("User-Agent: " + USER_AGENT) + "\n"
            command += ["--config", "-"]
            result = subprocess.run(command, input=config.encode(), capture_output=True)
            if result.returncode == 0:
                return result.stdout
        raise RuntimeError("Official creator URL request failed")


def resolve_sec_uid(profile: str, client: HttpClient) -> str:
    if SEC_UID.fullmatch(profile or ""):
        return profile
    match = re.search(r"https://[^\s]+", profile or "")
    if not match:
        raise ValueError("Provide an official HTTPS Douyin creator profile/share-card URL")
    current = match.group(0).rstrip(".,，。")
    for _ in range(8):
        parsed = official_url(current)
        match = re.fullmatch(r"/user/(MS4wLj[A-Za-z0-9_-]+)/?", parsed.path)
        if match:
            return match.group(1)
        query = parse_qs(parsed.query)
        for name in ("sec_uid", "sec_user_id"):
            values = query.get(name, [])
            if len(values) == 1 and SEC_UID.fullmatch(values[0]):
                return values[0]
        response = client.fetch(current, include_headers=True)
        header = response.split(b"\r\n\r\n", 1)[0].decode("latin1")
        redirects = re.findall(r"^location:\s*([^\r\n]+)", header, re.I | re.M)
        if not redirects:
            raise RuntimeError("Official share card did not reveal a creator identity or redirect")
        current = urljoin(current, redirects[-1].strip())
    raise RuntimeError("Too many official share-card redirects")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", help="creator share text, official profile URL, or sec_uid")
    parser.add_argument("--proxy", help="explicit proxy for public URL resolution")
    parser.add_argument("--dns-mode", choices=("auto", "off", "always"), default="auto")
    args = parser.parse_args()
    sec_uid = resolve_sec_uid(args.profile, HttpClient(proxy=args.proxy, dns_mode=args.dns_mode))
    print(json.dumps({"sec_uid": sec_uid, "profile_url": "https://www.douyin.com/user/" + sec_uid}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError):
        print(json.dumps({"event": "creator_identity_failed", "reason": "official_url_resolution_failed"}))
        raise SystemExit(2)
