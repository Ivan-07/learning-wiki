"""中文分词（jieba 封装）。

unicode61 会把连续汉字串当作单个 token，因此 FTS 主索引的写入与查询
都必须先经本模块分词、以空格连接。
"""

from __future__ import annotations

import threading

import jieba

_init_lock = threading.Lock()
_initialized = False


def _ensure_init() -> None:
    global _initialized
    if not _initialized:
        with _init_lock:
            if not _initialized:
                jieba.initialize()
                _initialized = True


def tokens(text: str) -> list[str]:
    """分词结果（去空、去重保序）。"""
    _ensure_init()
    seen: dict[str, None] = {}
    for tok in jieba.cut_for_search(text):
        tok = tok.strip()
        if tok:
            seen.setdefault(tok, None)
    return list(seen)


def segment(text: str) -> str:
    """分词后空格连接 —— FTS 索引列的写入格式。"""
    return " ".join(tokens(text))


def fts_query(text: str) -> str:
    """构建 FTS5 MATCH 表达式：每个 token 加引号（防注入），空格连接为 AND。"""
    toks = tokens(text)
    return " ".join(f'"{t}"' for t in toks)
