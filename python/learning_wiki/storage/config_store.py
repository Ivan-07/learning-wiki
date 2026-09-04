"""Config.yaml 的加载与保存。

- 位置：_System/Learning Wiki/Config.yaml（目录名来自 Config 本身 —— 不存在时
  回退到 init-vault 写入的默认位置标记）；
- 未知顶层键保留（round-trip），pydantic 只解析已知键。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from learning_wiki.domain.contracts import VaultConfig
from learning_wiki.storage import yaml_io


class ConfigNotFoundError(FileNotFoundError):
    pass


def config_path(system_dir: Path) -> Path:
    return system_dir / "Config.yaml"


def load_config(system_dir: Path) -> VaultConfig:
    path = config_path(system_dir)
    if not path.exists():
        raise ConfigNotFoundError(f"未找到 Config.yaml: {path}（先运行 lw init-vault）")
    data = yaml_io.load_yaml(path)
    return VaultConfig.model_validate(dict(data))


def save_config(system_dir: Path, config: VaultConfig, writer: Any = None) -> None:
    """保存时保留文件中已有但模型未知的顶层键。"""
    path = config_path(system_dir)
    known = config.model_dump(mode="json")
    if path.exists():
        existing = dict(yaml_io.load_yaml(path))
        unknown = {k: v for k, v in existing.items() if k not in known}
    else:
        unknown = {}
    yaml_io.dump_yaml({**known, **unknown}, path, writer)
