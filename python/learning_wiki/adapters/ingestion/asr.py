"""ASR 转写：把视频/音频的口播转成可检索文本。

实现选择（基于用户机器 Apple M1 + 16G 内存）：
- **本地 faster-whisper**，默认 ``medium`` 模型 + CPU + int8 量化 + **语言自动检测**。
  16G 内存下 large-v3（约 3G）偏重、CPU 推理慢；medium（约 1.5G）是
  质量与速度的平衡点。显存/内存充裕时可换 large-v3。
- faster-whisper 是 **可选依赖**（不写进 pyproject 主依赖），未安装时
  抛 ``AsrUnavailableError``，不阻塞主流程——同步 capture 流程不依赖本模块。

转写结果定位为「证据原文」，不追求逐字精校：
- 中文口播 large-v3 约 95%、medium 略低但口播场景足够；
- 同音字混淆、中英夹杂术语偶发错误属已知限制，不影响检索与记忆；
- 若日后要精校，走既有「纠正 = 新版本」机制追加，不改写历史。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path


class AsrUnavailableError(Exception):
    """ASR 引擎不可用（未安装 faster-whisper 或 ffmpeg/模型缺失）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# 默认配置：M1 16G 的平衡点
DEFAULT_MODEL_SIZE = "medium"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"
# 语言默认 **自动检测**，不硬编码 zh。
# 教训（实测）：对英文音轨强制 language="zh" 会得到错误语种标注，
# 且可能劣化转写质量——Whisper 的语言检测本身很准（中英文都可靠）。
# 中文场景的多数内容会被正确识别为 zh；确知语种时再显式指定。
DEFAULT_LANGUAGE: str | None = None
DEFAULT_BEAM_SIZE = 5

# 各模型 int8 量化的大致内存占用（GB），用于友好的配置校验提示
_MODEL_MEMORY_GB: dict[str, float] = {
    "tiny": 0.1,
    "base": 0.2,
    "small": 0.5,
    "medium": 1.5,
    "large-v2": 3.0,
    "large-v3": 3.0,
}

# 中文场景推荐：medium 起步，内存充裕可上 large-v3
_CHINESE_RECOMMENDED = ("medium", "large-v3")


@dataclass(frozen=True)
class AsrSegment:
    """一个转写片段（含时间信息，便于日后做时间锚点引用）。"""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class AsrResult:
    """一次转写的产出。"""

    text: str
    engine: str
    language: str | None = None
    language_probability: float | None = None
    duration_seconds: float | None = None
    confidence: float | None = None
    model_size: str | None = None
    segments: list[AsrSegment] = field(default_factory=list)

    def as_markdown(self, title: str = "转写文本") -> str:
        """转成可入库的 Markdown（供 EvidenceStore 分块建锚点）。"""
        lines = [f"# {title}\n"]
        if self.language:
            lang = f"识别语言：{self.language}"
            if self.language_probability is not None:
                lang += f"（置信度 {self.language_probability:.2f}）"
            lines.append(f"> {lang}\n")
        if self.duration_seconds:
            lines.append(f"> 时长：{self.duration_seconds / 60:.1f} 分钟\n")
        lines.append(f"\n{self.text}\n")
        return "\n".join(lines)


class AsrEngine:
    """转写引擎接口（Protocol 形态）。具体实现：LocalWhisperEngine 等。"""

    def transcribe(self, media_path: str, *, language: str | None = None) -> AsrResult:
        raise NotImplementedError


class LocalWhisperEngine(AsrEngine):
    """本地 faster-whisper 引擎。

    可选依赖：``pip install faster-whisper``。模型权重首次使用时自动下载
    （medium 约 1.5GB，large-v3 约 3GB），缓存到本地。

    M1/无 NVIDIA 显卡 → 走 CPU + int8；有 CUDA 时可改 device="cuda" +
    compute_type="float16" 获得数倍提速。
    """

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        *,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        beam_size: int = DEFAULT_BEAM_SIZE,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self._model: object | None = None

    @property
    def engine_name(self) -> str:
        return f"faster-whisper:{self.model_size}:{self.compute_type}"

    def _load_model(self) -> object:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise AsrUnavailableError(
                "未安装 faster-whisper，请执行：pip install faster-whisper"
            ) from exc
        try:
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        except Exception as exc:
            raise AsrUnavailableError(f"加载 Whisper 模型失败: {exc}") from exc
        return self._model

    def transcribe(self, media_path: str, *, language: str | None = None) -> AsrResult:
        path = Path(media_path)
        if not path.exists():
            raise FileNotFoundError(f"媒体文件不存在: {path}")

        model = self._load_model()
        lang = language or DEFAULT_LANGUAGE

        segments_gen, info = model.transcribe(  # type: ignore[attr-defined]
            str(path),
            language=lang,
            beam_size=self.beam_size,
        )

        segments: list[AsrSegment] = []
        texts: list[str] = []
        last_end = 0.0
        for seg in segments_gen:
            text = (seg.text or "").strip()
            if text:
                segments.append(AsrSegment(start=float(seg.start), end=float(seg.end), text=text))
                texts.append(text)
                last_end = max(last_end, float(seg.end))

        return AsrResult(
            text="\n".join(texts),
            engine=self.engine_name,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            duration_seconds=getattr(info, "duration", None) or last_end or None,
            model_size=self.model_size,
            segments=segments,
        )


def check_available() -> tuple[bool, str]:
    """检查本地 ASR 是否可用，返回 (可用, 说明)。用于 CLI 友好提示。"""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, "未安装 faster-whisper（pip install faster-whisper）"
    if shutil.which("ffmpeg") is None:
        # faster-whisper 用内置 PyAV 解码，不强依赖系统 ffmpeg；
        # 但仍提示一下，便于后续做音频抽取。
        return True, "faster-whisper 已安装（系统 ffmpeg 缺失，仅影响手工抽音轨）"
    return True, "faster-whisper 已安装"


def recommend_model(total_memory_gb: float) -> str:
    """按可用内存推荐模型大小（中文场景）。"""
    if total_memory_gb >= 32:
        return "large-v3"
    if total_memory_gb >= 16:
        return "medium"
    return "small"


def estimate_memory_gb(model_size: str) -> float:
    return _MODEL_MEMORY_GB.get(model_size, 1.5)
