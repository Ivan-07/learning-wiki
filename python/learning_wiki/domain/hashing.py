"""内容规范化与哈希。

规范化规则（content_hash 与 span_hash 共用）：
1. 去 BOM；
2. 统一换行为 LF；
3. NFKC 规范化；
4. 去除每行行尾空白；
5. 去除首尾空白。

哈希前缀 ``sha256:``。篡改文件但伪造时间戳无法逃过内容哈希（规格 13.3.6）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_PREFIX = "sha256:"


def normalize_text(text: str) -> str:
    import unicodedata

    text = text.removeprefix("﻿")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFKC", text)
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def hash_text(text: str) -> str:
    normalized = normalize_text(text)
    return _PREFIX + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# span_hash 与 content_hash 目前共用同一规范化；独立函数保留扩展空间
# （未来可对块级文本采用更宽松的规范化）。
def span_hash(text: str) -> str:
    return hash_text(text)


def hash_bytes(data: bytes) -> str:
    return _PREFIX + hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    return _PREFIX + hashlib.sha256(path.read_bytes()).hexdigest()


def strip_prefix(value: str) -> str:
    return value.removeprefix(_PREFIX)
