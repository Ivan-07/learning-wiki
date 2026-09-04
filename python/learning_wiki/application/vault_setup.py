"""init-vault：目录、Config、Agent 规则与派生库的初始化。

- 幂等：已存在的文件与目录一律不动（不移动用户既有笔记）；
- 模板来自包内 learning_wiki.vault_package（AGENTS.md / CLAUDE.md /
  config.default.yaml）；
- 若 Vault 是 Git 仓库，确保 .learning-wiki/ 被排除（只追加，不改已有内容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from learning_wiki.adapters.executor.local_fsync import SafeFileWriter
from learning_wiki.domain.contracts import VaultConfig
from learning_wiki.storage import config_store
from learning_wiki.storage.db import connection as db_connection
from learning_wiki.storage.vault_paths import VaultPaths

FOLDER_NAMES = [
    "inbox",
    "sources",
    "thoughts",
    "wiki",
    "learning",
    "projects",
    "archive",
    "system",
]
DOT_SUBDIRS = ["cache", "locks", "staging", "runtime"]
# 40 Learning/ 下的学习机制子目录（幂等创建）
LEARNING_SUBDIRS = ["Goals", "Objects", "Challenges", "Misconceptions", "Events", "Views"]


@dataclass
class InitResult:
    root: Path
    created_dirs: list[str] = field(default_factory=list)
    wrote_files: list[str] = field(default_factory=list)
    migrations_applied: list[int] = field(default_factory=list)


def _template(name: str) -> str:
    return (resources.files("learning_wiki.vault_package") / name).read_text(encoding="utf-8")


class VaultSetup:
    def init(self, root: Path, config: VaultConfig | None = None) -> InitResult:
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        result = InitResult(root=root)
        writer = SafeFileWriter(root)

        paths = VaultPaths(root, config or VaultConfig())

        for name in FOLDER_NAMES:
            folder = paths.folder(name)
            if not folder.exists():
                folder.mkdir(parents=True)
                result.created_dirs.append(self._rel(root, folder))

        # 学习机制子目录（Goals/Objects/Challenges/Misconceptions/Events/Views）
        learning_dir = paths.folder("learning")
        for sub in LEARNING_SUBDIRS:
            d = learning_dir / sub
            if not d.exists():
                d.mkdir(parents=True)
                result.created_dirs.append(self._rel(root, d))

        for sub in DOT_SUBDIRS:
            d = paths.dot_dir() / sub
            if not d.exists():
                d.mkdir(parents=True)

        # Config.yaml：不存在才写（保留用户自定义）
        cfg_path = config_store.config_path(paths.system_dir())
        if not cfg_path.exists():
            writer.write_text(cfg_path, _template("config.default.yaml"))
            result.wrote_files.append(self._rel(root, cfg_path))

        # Agent 规则：AGENTS.md 与 CLAUDE.md 语义等价
        for name in ("AGENTS.md", "CLAUDE.md"):
            target = root / name
            if not target.exists():
                writer.write_text(target, _template(name))
                result.wrote_files.append(self._rel(root, target))

        # Git 排除：仅当已是 Git 仓库且有 .gitignore 时追加
        self._ensure_dot_dir_excluded(root, writer, result)

        # 派生库
        conn = db_connection.connect(paths.db_path())
        try:
            result.migrations_applied = db_connection.migrate(conn)
        finally:
            conn.close()
        return result

    def _ensure_dot_dir_excluded(
        self, root: Path, writer: SafeFileWriter, result: InitResult
    ) -> None:
        gitignore = root / ".gitignore"
        if not ((root / ".git").exists() or (root / ".git").is_dir()):
            return
        if not gitignore.exists():
            writer.write_text(gitignore, ".learning-wiki/\n")
            result.wrote_files.append(".gitignore")
            return
        content = gitignore.read_text(encoding="utf-8")
        if ".learning-wiki" not in content:
            writer.write_text(gitignore, content.rstrip("\n") + "\n.learning-wiki/\n")

    @staticmethod
    def _rel(root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix()
