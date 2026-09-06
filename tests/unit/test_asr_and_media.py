"""ASR 与媒体流单元测试。

faster-whisper 是可选依赖、测试环境不装模型，因此引擎用注入的假模型测试；
媒体流用假 fetcher 喂 playurl JSON，不真实触网。
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from learning_wiki.adapters.ingestion import asr
from learning_wiki.adapters.ingestion.media import (
    AudioStream,
    BiliMediaFetcher,
    MediaStreamError,
    pick_audio_stream,
)
from learning_wiki.adapters.net_guard import FetchedPage

# ---------------------------------------------------------------------------
# ASR 引擎
# ---------------------------------------------------------------------------


class _FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class _FakeInfo:
    language = "zh"
    language_probability = 0.99
    duration = 12.0


class _FakeModel:
    """假 faster-whisper 模型：记录调用参数，返回预设片段。"""

    def __init__(self, segments: list[_FakeSegment]) -> None:
        self.segments = segments
        self.calls: list[dict] = []

    def transcribe(self, path: str, **kwargs):
        self.calls.append({"path": path, **kwargs})
        return iter(self.segments), _FakeInfo()


def _install_fake_whisper(monkeypatch, model: _FakeModel) -> None:
    """把假的 faster_whisper 模块塞进 sys.modules，模拟可选依赖已安装。"""
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = lambda *a, **k: model
    monkeypatch.setitem(sys.modules, "faster_whisper", module)


class TestLocalWhisperEngine:
    def test_transcribe_returns_segments_and_text(self, monkeypatch, tmp_path) -> None:
        model = _FakeModel(
            [
                _FakeSegment(0.0, 3.0, "第一段内容"),
                _FakeSegment(3.0, 6.0, "第二段内容"),
                _FakeSegment(6.0, 9.0, "   "),  # 空白段应被丢弃
            ]
        )
        _install_fake_whisper(monkeypatch, model)

        audio = tmp_path / "a.m4s"
        audio.write_bytes(b"fake-audio")

        engine = asr.LocalWhisperEngine(model_size="medium")
        result = engine.transcribe(str(audio))

        assert result.language == "zh"
        assert result.language_probability == pytest.approx(0.99)
        assert result.duration_seconds == 12.0
        assert result.model_size == "medium"
        assert result.text == "第一段内容\n第二段内容"
        assert len(result.segments) == 2  # 空白段被过滤
        assert result.segments[0].text == "第一段内容"

    def test_auto_language_by_default(self, monkeypatch, tmp_path) -> None:
        """默认不硬编码语种：未知内容交给 Whisper 自动检测。

        实测教训：英文音轨强制 zh 会得到错误语种标注并劣化质量。
        """
        model = _FakeModel([_FakeSegment(0.0, 2.0, "hello")])
        _install_fake_whisper(monkeypatch, model)

        audio = tmp_path / "a.m4s"
        audio.write_bytes(b"x")

        engine = asr.LocalWhisperEngine()
        engine.transcribe(str(audio))

        assert model.calls[0]["language"] is None  # 即 auto

    def test_explicit_language_passed_through(self, monkeypatch, tmp_path) -> None:
        model = _FakeModel([_FakeSegment(0.0, 2.0, "你好")])
        _install_fake_whisper(monkeypatch, model)

        audio = tmp_path / "a.m4s"
        audio.write_bytes(b"x")

        engine = asr.LocalWhisperEngine()
        engine.transcribe(str(audio), language="zh")

        assert model.calls[0]["language"] == "zh"

    def test_defaults_are_cpu_int8(self) -> None:
        engine = asr.LocalWhisperEngine()
        # M1 无 CUDA：走 CPU + int8 量化
        assert engine.device == "cpu"
        assert engine.compute_type == "int8"
        assert engine.model_size == "medium"

    def test_engine_name_identifies_config(self) -> None:
        engine = asr.LocalWhisperEngine("large-v3")
        assert engine.engine_name == "faster-whisper:large-v3:int8"

    def test_missing_dependency_raises(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        audio = tmp_path / "a.m4s"
        audio.write_bytes(b"x")
        engine = asr.LocalWhisperEngine()
        with pytest.raises(asr.AsrUnavailableError) as exc:
            engine.transcribe(str(audio))
        assert "faster-whisper" in str(exc.value)

    def test_missing_file_raises(self) -> None:
        engine = asr.LocalWhisperEngine()
        with pytest.raises(FileNotFoundError):
            engine.transcribe("/nonexistent/audio.m4s")


class TestAsrResult:
    def test_as_markdown_for_ingestion(self) -> None:
        result = asr.AsrResult(
            text="口播正文",
            engine="faster-whisper:medium:int8",
            language="zh",
            language_probability=0.98,
            duration_seconds=600,
        )
        md = result.as_markdown(title="某视频口播")
        assert "# 某视频口播" in md
        assert "识别语言：zh" in md
        assert "口播正文" in md
        # 结果应能被 EvidenceStore 正常分块建锚点
        from learning_wiki.storage import evidence_store

        assert evidence_store.split_markdown_blocks(md)


class TestHelpers:
    @pytest.mark.parametrize(
        ("memory_gb", "expected"),
        [(64, "large-v3"), (32, "large-v3"), (16, "medium"), (8, "small")],
    )
    def test_recommend_model(self, memory_gb: int, expected: str) -> None:
        assert asr.recommend_model(memory_gb) == expected

    def test_estimate_memory(self) -> None:
        assert asr.estimate_memory_gb("large-v3") == 3.0
        assert asr.estimate_memory_gb("unknown") == 1.5  # 保守默认值

    def test_check_available_when_not_installed(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        ok, msg = asr.check_available()
        assert ok is False
        assert "pip install faster-whisper" in msg


# ---------------------------------------------------------------------------
# 媒体流
# ---------------------------------------------------------------------------


class _FakeFetcher:
    def __init__(self, responses: dict[str, FetchedPage]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchedPage:
        self.calls.append((url, headers))
        if url not in self.responses:
            raise AssertionError(f"未预设的 URL: {url}")
        return self.responses[url]


def _playurl_data() -> dict:
    """playurl API 的内层 data。"""
    return {
        "dash": {
            "duration": 600,
            "audio": [
                {
                    "id": 30232,
                    "bandwidth": 134695,
                    "codecs": "mp4a.40.2",
                    "baseUrl": "https://cdn.example.com/high.m4s",
                },
                {
                    "id": 30216,
                    "bandwidth": 68646,
                    "codecs": "mp4a.40.2",
                    "baseUrl": "https://cdn.example.com/low.m4s",
                },
            ],
        }
    }


def _playurl_response(data: dict | None = None, code: int = 0) -> bytes:
    """真实 API 形态：外层 {code, message, data}。"""
    return json.dumps(
        {
            "code": code,
            "message": "0" if code == 0 else "error",
            "data": _playurl_data() if data is None else data,
        }
    ).encode()


class TestPickAudioStream:
    def test_prefer_lowest_bandwidth(self) -> None:
        stream = pick_audio_stream(_playurl_data())
        # 转写不需要高音质，选最低码率省流量
        assert stream.bandwidth == 68646
        assert stream.stream_id == 30216
        assert stream.url == "https://cdn.example.com/low.m4s"

    def test_prefer_highest(self) -> None:
        stream = pick_audio_stream(_playurl_data(), prefer_lowest=False)
        assert stream.bandwidth == 134695

    def test_no_dash_raises(self) -> None:
        with pytest.raises(MediaStreamError):
            pick_audio_stream({"durl": []})

    def test_no_audio_raises(self) -> None:
        with pytest.raises(MediaStreamError):
            pick_audio_stream({"dash": {"audio": []}})


class TestBiliMediaFetcher:
    def test_list_streams(self) -> None:
        fetcher = _FakeFetcher(
            {
                "https://api.bilibili.com/x/player/playurl?bvid=BV1&cid=1&fnval=16": _page(
                    _playurl_response(), "u"
                )
            }
        )
        streams = BiliMediaFetcher(fetcher=fetcher).list_audio_streams("BV1", 1)  # type: ignore[arg-type]
        assert len(streams) == 2
        assert all(isinstance(s, AudioStream) for s in streams)

    def test_fetch_audio_uses_lowest_and_referer(self) -> None:
        responses = {
            "https://api.bilibili.com/x/player/playurl?bvid=BV1&cid=1&fnval=16": _page(
                _playurl_response(), "u"
            ),
            "https://cdn.example.com/low.m4s": _page(b"audio-bytes", "low"),
        }
        fetcher = _FakeFetcher(responses)
        audio = BiliMediaFetcher(fetcher=fetcher).fetch_audio("BV1", 1)  # type: ignore[arg-type]

        assert audio.content == b"audio-bytes"
        assert audio.bandwidth == 68646  # 最低码率
        # B站 CDN 必须带 Referer，否则 403
        audio_call = next(c for c in fetcher.calls if c[0].endswith("low.m4s"))
        assert audio_call[1] == {"Referer": "https://www.bilibili.com/"}

    def test_api_error_raises(self) -> None:
        fetcher = _FakeFetcher(
            {
                "https://api.bilibili.com/x/player/playurl?bvid=BV1&cid=1&fnval=16": _page(
                    _playurl_response(code=-404), "u"
                )
            }
        )
        from learning_wiki.adapters.net_guard import CaptureBlockedError

        with pytest.raises(CaptureBlockedError):
            BiliMediaFetcher(fetcher=fetcher).fetch_audio("BV1", 1)  # type: ignore[arg-type]


def _page(content: bytes, url: str, status: int = 200) -> FetchedPage:
    return FetchedPage(url=url, status=status, headers={}, content=content)
