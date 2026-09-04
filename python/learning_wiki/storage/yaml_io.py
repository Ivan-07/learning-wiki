"""YAML 读写（round-trip：保留注释与未知字段）。

所有 Vault 内 YAML 的事实源读写必须经过本模块：
- load 返回 ruamel CommentedMap（未识别的键与注释原样保留）；
- dump 前后未知字段集合不变由 lint 检查（规格 §5「不允许静默删除未来字段」）。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.width = 4096
    y.allow_unicode = True
    return y


def load_yaml(path: Path) -> CommentedMap:
    loaded = _yaml().load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, CommentedMap)
    return loaded


def load_yaml_str(text: str) -> Any:
    """round-trip 模式解析字符串（保留注释与键序）。"""
    return _yaml().load(text)


def dump_yaml_str(data: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def dump_yaml(data: Any, path: Path, writer: Any = None) -> None:
    """写出 YAML；若提供 writer（SafeFileWriter）则原子写。"""
    text = dump_yaml_str(data)
    if writer is not None:
        writer.write_text(path, text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
