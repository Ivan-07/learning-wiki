"""SSRF 对抗测试（规格 13.3.3）。

GuardedFetcher 默认拒绝环回/私网/链路本地/云元数据；重定向逐跳重校验。
"""

import threading
from http.server import BaseHTTPRequestHandler

import pytest

from learning_wiki.adapters.net_guard import GuardedFetcher, SSRFBlockedError

BLOCKED_TARGETS = [
    "http://127.0.0.1/x",
    "http://localhost/x",
    "http://10.0.0.1/x",
    "http://192.168.1.1/x",
    "http://172.16.0.1/x",
    "http://169.254.169.254/latest/meta-data/",  # 云元数据
    "http://[fd00::1]/x",
    "http://[::ffff:10.0.0.1]/x",  # IPv4-mapped
    "file:///etc/passwd",
    "ftp://example.com/x",
    "http://0.0.0.0/x",
]


@pytest.mark.parametrize("url", BLOCKED_TARGETS)
def test_blocked_targets(url: str) -> None:
    fetcher = GuardedFetcher()
    with pytest.raises(SSRFBlockedError):
        fetcher.fetch(url)


def test_private_ip_hostname_resolution_blocked(monkeypatch) -> None:
    """域名解析到私网 IP 也被拦截。"""
    import socket

    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    fetcher = GuardedFetcher()
    with pytest.raises(SSRFBlockedError):
        fetcher._validate_host("evil.example.com")


def test_redirect_to_private_blocked():
    """公网（此处用允许的环回模拟第一跳）→ 重定向跳私网：被拦截。"""

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/hop1":
                self.send_response(302)
                self.send_header("Location", "http://10.0.0.1/hop2")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fetcher = GuardedFetcher(allow_loopback=True)  # 第一跳允许环回
        with pytest.raises(SSRFBlockedError) as exc_info:
            fetcher.fetch(f"http://127.0.0.1:{port}/hop1")
        assert "10.0.0.1" in str(exc_info.value)
    finally:
        server.shutdown()
