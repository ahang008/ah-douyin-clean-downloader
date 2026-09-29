#!/usr/bin/env python3
"""Run a normal, dedicated Edge process for official creator-page collection.

The launcher never extracts the daily browser's storage or Keychain material.
Edge handles a new normal login in its own profile. Only our child group closes.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
EDGE = Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge')


def emit(event: str, **values) -> None:
    print(json.dumps({'event': event, **values}, ensure_ascii=False), flush=True)


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        os.chmod(temporary, 0o600)
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(temporary, path)


def validate_profile(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'www.douyin.com' or parsed.query or parsed.fragment:
        raise ValueError('Use the canonical official Douyin creator profile URL')
    if not re.fullmatch(r'/user/MS4wLj[A-Za-z0-9_-]+/?', parsed.path):
        raise ValueError('An official creator sec_uid profile is required')
    return url.rstrip('/')


def prepare_session(path: Path) -> None:
    forbidden = (Path.home() / 'Library/Application Support/Microsoft Edge').resolve()
    if path == forbidden or forbidden in path.parents:
        raise ValueError('Refused the existing daily Edge profile')
    marker = path / 'session-owner.json'
    if path.exists() and any(path.iterdir()) and not marker.is_file():
        raise ValueError('A nonempty session directory must have this launcher ownership marker')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    if marker.is_file():
        saved = json.loads(marker.read_text())
        if saved.get('purpose') != 'douyin_creator_public_catalog':
            raise ValueError('Session directory belongs to another purpose')
    else:
        atomic_json(marker, {'schema_version': 1, 'purpose': 'douyin_creator_public_catalog',
                             'created_at': datetime.now(timezone.utc).isoformat()})


def available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def needs_dns_tunnel(mode: str) -> bool:
    """Keep normal browser networking unless fake DNS repair is requested/needed."""
    if mode == 'off':
        return False
    if mode == 'always':
        return True
    if mode != 'auto':
        raise ValueError('dns-mode must be auto, off, or always')
    try:
        addresses = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo('www.douyin.com', 443)]
        return any(address.version == 4 and address in ipaddress.ip_network('198.18.0.0/15')
                   for address in addresses)
    except socket.gaierror:
        return True


def wait_for_port(process: subprocess.Popen, profile: Path, timeout: float = 30,
                  expected_port: int | None = None) -> int:
    deadline = time.monotonic() + timeout
    port_file = profile / 'DevToolsActivePort'
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Dedicated Edge exited before the local connection was ready')
        try:
            port = expected_port if expected_port is not None else int(port_file.read_text().splitlines()[0])
            if 1 <= port <= 65535:
                result = subprocess.run(['lsof', '-nP', '-a', '-p', str(process.pid),
                                         '-iTCP:' + str(port), '-sTCP:LISTEN', '-Fn'],
                                        capture_output=True, text=True, timeout=5)
                addresses = [line[1:] for line in result.stdout.splitlines() if line.startswith('n')]
                if addresses and all(address in ('127.0.0.1:' + str(port), '[::1]:' + str(port))
                                     for address in addresses):
                    return port
                if addresses:
                    raise RuntimeError('Refused a non-loopback debugging listener')
        except (FileNotFoundError, ValueError, IndexError):
            pass
        time.sleep(0.2)
    raise RuntimeError('Dedicated Edge local connection did not become ready')


def stop_owned(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    # start_new_session makes this group unique to the child we created.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def probe(endpoint: str, profile_url: str, report_path: Path) -> int:
    for name in ('DEBUG', 'DEBUG_FILE', 'PWDEBUG'):
        os.environ.pop(name, None)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint, no_defaults=True)
        context = browser.contexts[0]
        page = context.new_page()
        status = None
        navigation_error = None
        try:
            response = page.goto(profile_url, wait_until='domcontentloaded', timeout=45000)
            status = response.status if response else None
            page.wait_for_timeout(6000)
        except Exception as exc:
            navigation_error = type(exc).__name__
        # Only normal public page text and a local screenshot, never browser storage.
        body = ''
        body_error = None
        try:
            body = page.locator('body').inner_text(timeout=5000)
        except Exception as exc:
            body_error = type(exc).__name__
        screenshot = report_path.with_suffix('.png')
        screenshot_error = None
        try:
            page.screenshot(path=str(screenshot), full_page=False, timeout=5000)
            os.chmod(screenshot, 0o600)
        except Exception as exc:
            screenshot_error = type(exc).__name__
        data = {'schema_version': 1, 'http_status': status, 'page_title': page.title(),
                'navigator_webdriver': page.evaluate('() => navigator.webdriver'),
                'navigation_error_type': navigation_error, 'page_text_excerpt': body[:300],
                'body_error_type': body_error, 'screenshot_error_type': screenshot_error,
                'login_text_present': '登录' in body, 'screenshot': str(screenshot),
                'existing_edge_storage_read': False, 'keychain_cli_calls': 0}
        atomic_json(report_path, data)
        emit('browser_probe', http_status=status, page_title=data['page_title'],
             navigation_error_type=navigation_error, body_error_type=body_error,
             report=str(report_path), screenshot=str(screenshot) if screenshot.is_file() else None)
        return 0 if status and 200 <= status < 400 and not navigation_error and body else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--session-dir', type=Path)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--dns-mode', choices=('auto', 'off', 'always'), default='auto',
                        help='auto keeps normal networking unless official host DNS is fake or unavailable')
    parser.add_argument('--direct-network', action='store_true', help='compatibility alias for --dns-mode off')
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    profile_url = validate_profile(args.profile)
    output = args.output.expanduser().resolve()
    session = (args.session_dir.expanduser().resolve() if args.session_dir
               else output.parent.parent / '.browser-session')
    if not EDGE.is_file():
        raise RuntimeError('The installed official Microsoft Edge executable was not found')
    if not 30 <= args.timeout <= 3600 or not 1 <= args.max_pages <= 1000:
        raise ValueError('timeout must be 30–3600 seconds and max-pages 1–1000')
    prepare_session(session)
    lock = (session / '.launcher.lock').open('a')
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    proxy = process = collector = None
    try:
        child_env = dict(os.environ)
        for name in ('DEBUG', 'DEBUG_FILE', 'PWDEBUG'):
            child_env.pop(name, None)
        child_env['NO_PROXY'] = '127.0.0.1,localhost,::1'
        selected_port = available_loopback_port()
        arguments = [str(EDGE), '--user-data-dir=' + str(session),
                     '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + str(selected_port),
                     '--no-first-run', 'about:blank']
        if needs_dns_tunnel('off' if args.direct_network else args.dns_mode):
            from browser_dns_proxy import BrowserDNSProxy
            proxy = BrowserDNSProxy()
            proxy_port = proxy.start()
            arguments.insert(-1, '--proxy-server=http://127.0.0.1:' + str(proxy_port))
        (session / 'DevToolsActivePort').unlink(missing_ok=True)
        process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, env=child_env)
        # A competing process can take the released port. Never connect unless
        # our exact child PID owns only loopback listeners on that port.
        port = wait_for_port(process, session, expected_port=selected_port)
        endpoint = 'http://127.0.0.1:' + str(port)
        atomic_json(session / 'launcher-status.json', {'pid': process.pid, 'port': port,
                    'session_dir': str(session), 'normal_edge_startup': True,
                    'loopback_listener_verified': True, 'existing_profile_read': False})
        emit('dedicated_browser_ready', message='专用 Edge 已打开。如官方页面需要登录，请用抖音正常扫码登录。',
             normal_edge_startup=True, loopback_listener_verified=True)
        if args.probe:
            return probe(endpoint, profile_url, output)
        command = [sys.executable, str(HERE / 'collect_creator_browser.py'),
                   '--profile', profile_url, '--cdp', endpoint, '--output', str(output),
                   '--timeout', str(args.timeout), '--max-pages', str(args.max_pages),
                   '--browser-pid', str(process.pid), '--user-data-dir', str(session),
                   '--show-login']
        collector = subprocess.Popen(command, env=child_env)
        return collector.wait()
    except KeyboardInterrupt:
        if collector and collector.poll() is None:
            collector.send_signal(signal.SIGINT)
            collector.wait(timeout=10)
        emit('interrupted', message='专用会话和已有文件保留，可重新续跑。')
        return 130
    finally:
        try:
            if collector and collector.poll() is None:
                collector.terminate()
                try:
                    collector.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    collector.kill()
                    collector.wait(timeout=3)
        finally:
            try:
                stop_owned(process)
            finally:
                try:
                    if proxy:
                        try:
                            atomic_json(session / 'dns-tunnel-status.json', proxy.stats())
                        finally:
                            proxy.stop()
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    lock.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never echo signed URLs or credential-bearing third-party error messages.
        emit('browser_launch_failed', error_type=type(exc).__name__,
             message=str(exc) if isinstance(exc, ValueError) else 'See the dedicated browser status; existing files were retained.')
        raise SystemExit(2)
