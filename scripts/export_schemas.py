"""从 domain/contracts.py 导出 JSON Schema 到 schemas/。

用法：
    python scripts/export_schemas.py           # 写出
    python scripts/export_schemas.py --check   # CI 漂移检查（不写，只比对）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO / "schemas"

sys.path.insert(0, str(REPO / "python"))

from learning_wiki.domain.contracts import EXPORTED_MODELS  # noqa: E402


def render(name: str, model: type) -> str:
    schema = model.model_json_schema()
    schema["$id"] = f"https://learning-wiki.local/schemas/{name}.schema.json"
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    check = "--check" in sys.argv
    SCHEMAS_DIR.mkdir(exist_ok=True)
    drift = False
    for name, model in sorted(EXPORTED_MODELS.items()):
        rendered = render(name, model)
        target = SCHEMAS_DIR / f"{name}.schema.json"
        if check:
            if not target.exists() or target.read_text(encoding="utf-8") != rendered:
                print(f"schema drift: {target.name}（请运行 `make schemas`）")
                drift = True
        else:
            target.write_text(rendered, encoding="utf-8")
            print(f"wrote {target}")
    if check and drift:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
