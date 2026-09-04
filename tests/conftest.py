"""测试基建：vault 工厂、FrozenClock、黄金样本路径、本地 Web 服务器。"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from datetime import datetime
from pathlib import Path

import pytest

from learning_wiki.adapters.capture.plain_text import PlainTextAdapter
from learning_wiki.adapters.capture.text_file import TextFileAdapter
from learning_wiki.adapters.capture.web import GenericWebAdapter
from learning_wiki.adapters.net_guard import GuardedFetcher
from learning_wiki.application.capture_service import CaptureService
from learning_wiki.application.context import VaultContext
from learning_wiki.application.vault_setup import VaultSetup
from learning_wiki.domain.clock import FrozenClock
from learning_wiki.domain.contracts import VaultConfig

FIXTURES = Path(__file__).parent / "fixtures"


def make_vault(
    root: Path,
    *,
    clock: FrozenClock | None = None,
    config: VaultConfig | None = None,
) -> VaultContext:
    """初始化一个测试 Vault 并返回装配好的 VaultContext。"""
    VaultSetup().init(root, config=config)
    return VaultContext(root, clock=clock)


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 9, 3, 10, 30, 0))


@pytest.fixture
def vault(tmp_path: Path, frozen_clock: FrozenClock) -> VaultContext:
    ctx = make_vault(tmp_path / "vault", clock=frozen_clock)
    yield ctx
    ctx.close()


class FixtureWebServer:
    """本地静态服务器（服务 tests/fixtures；测试例外：允许环回）。"""

    def __init__(self) -> None:
        handler = functools.partial(_FixtureHandler, directory=str(FIXTURES))
        self.server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, name: str) -> str:
        return f"{self.base_url}/{name}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _FixtureHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # 静音
        pass


@pytest.fixture
def web_server():
    server = FixtureWebServer()
    yield server
    server.stop()


@pytest.fixture
def capture_with_web(vault: VaultContext, web_server: FixtureWebServer) -> CaptureService:
    """带本地 Web 适配器（允许环回的 GuardedFetcher）的 CaptureService。"""
    return CaptureService(
        paths=vault.paths,
        config=vault.config,
        repo=vault.source_repository,
        evidence=vault.evidence_service,
        indexer=vault.indexer,
        fts=vault.fts,
        conn=vault.conn,
        clock=vault.clock,
        writer=vault.writer,
        adapters={
            "text": PlainTextAdapter(),
            "file": TextFileAdapter(),
            "url": GenericWebAdapter(fetcher=GuardedFetcher(allow_loopback=True)),
        },
    )


def fixture_path(name: str) -> Path:
    return FIXTURES / name
