"""媒体流获取：从视频平台拿可转写的音频。

B站链路（实测可行，无需 WBI 签名）：
1. ``x/web-interface/view`` → 拿 cid（各分P的播放流 ID）；
2. ``x/player/playurl?bvid&cid&fnval=16`` → 拿 DASH 清单；
3. 从 dash.audio 选一路音频（**默认最低码率**：转写不需要高音质，省流量省时间）；
4. 下载音频字节（**必须带 Referer**，否则 CDN 返回 403）。

产出：音频字节 + 元信息，交给 asr.LocalWhisperEngine 转写。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from learning_wiki.adapters.net_guard import CaptureBlockedError, GuardedFetcher

_PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
# B站 CDN 校验 Referer，缺了直接 403
_BILI_REFERER = "https://www.bilibili.com/"

# fnval=16 → DASH 格式（音视频分离，可只下音频）
_FNVAL_DASH = 16


@dataclass(frozen=True)
class AudioStream:
    """一路可选音频流。"""

    stream_id: int
    bandwidth: int
    codecs: str
    url: str


@dataclass(frozen=True)
class FetchedAudio:
    """已下载的音频。"""

    content: bytes
    stream_id: int
    bandwidth: int
    codecs: str
    suffix: str = ".m4s"


class MediaStreamError(Exception):
    pass


def pick_audio_stream(playurl_data: dict, *, prefer_lowest: bool = True) -> AudioStream:
    """从 playurl 返回的 DASH 清单里选一路音频。

    默认选最低码率：ASR 转写对音质不敏感（16kbps 电话音质即可），
    最低码率能显著减少下载量与耗时。
    """
    dash = (playurl_data or {}).get("dash")
    if not dash:
        raise MediaStreamError("播放地址不含 DASH 清单（可能是仅 MP4 源）")
    audio = dash.get("audio") or []
    if not audio:
        raise MediaStreamError("DASH 清单中没有音频流")

    def bandwidth(item: dict) -> int:
        return int(item.get("bandwidth") or 0)

    chosen = min(audio, key=bandwidth) if prefer_lowest else max(audio, key=bandwidth)
    url = chosen.get("baseUrl") or chosen.get("base_url")
    if not url:
        raise MediaStreamError("音频流缺少 URL")
    return AudioStream(
        stream_id=int(chosen.get("id") or 0),
        bandwidth=bandwidth(chosen),
        codecs=chosen.get("codecs") or "",
        url=url,
    )


class BiliMediaFetcher:
    """B站音频流获取。"""

    def __init__(self, fetcher: GuardedFetcher | None = None) -> None:
        self.fetcher = fetcher or GuardedFetcher()

    def list_audio_streams(self, bvid: str, cid: int) -> list[AudioStream]:
        """列出可用音频流（供上层选择或展示）。"""
        data = self._fetch_playurl(bvid, cid)
        dash = (data or {}).get("dash") or {}
        streams: list[AudioStream] = []
        for item in dash.get("audio") or []:
            url = item.get("baseUrl") or item.get("base_url")
            if not url:
                continue
            streams.append(
                AudioStream(
                    stream_id=int(item.get("id") or 0),
                    bandwidth=int(item.get("bandwidth") or 0),
                    codecs=item.get("codecs") or "",
                    url=url,
                )
            )
        if not streams:
            raise MediaStreamError("该视频没有可用音频流")
        return streams

    def fetch_audio(self, bvid: str, cid: int, *, prefer_lowest: bool = True) -> FetchedAudio:
        """下载一路音频（默认最低码率）。"""
        streams = self.list_audio_streams(bvid, cid)
        chosen = (
            min(streams, key=lambda s: s.bandwidth)
            if prefer_lowest
            else max(streams, key=lambda s: s.bandwidth)
        )
        page = self.fetcher.fetch(chosen.url, headers={"Referer": _BILI_REFERER})
        if page.status != 200:
            raise CaptureBlockedError(f"音频下载失败：HTTP {page.status}")
        if not page.content:
            raise MediaStreamError("音频内容为空")
        return FetchedAudio(
            content=page.content,
            stream_id=chosen.stream_id,
            bandwidth=chosen.bandwidth,
            codecs=chosen.codecs,
        )

    def _fetch_playurl(self, bvid: str, cid: int) -> dict:
        url = f"{_PLAYURL_API}?bvid={bvid}&cid={cid}&fnval={_FNVAL_DASH}"
        page = self.fetcher.fetch(url, headers={"Referer": _BILI_REFERER})
        if page.status != 200:
            raise CaptureBlockedError(f"playurl 返回 HTTP {page.status}")
        body = json.loads(page.content.decode("utf-8", errors="replace"))
        if body.get("code") != 0:
            raise CaptureBlockedError(f"playurl 错误: {body.get('message', body.get('code'))}")
        return body.get("data") or {}
