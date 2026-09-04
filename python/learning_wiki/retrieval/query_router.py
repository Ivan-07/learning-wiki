"""查询路由（规格 6.4）。

M1 实现 exact / source_local / conceptual 三类；
temporal / personal / action 留待后续里程碑。
"""

from __future__ import annotations

import re

_ID_PATTERN = re.compile(r"^(src_[0-9A-HJKMNP-TV-Z]{26}|ev_[0-9A-Za-z]+_v\d{4}_\d{4})$")


def classify_query(query: str, *, source_id: str | None = None) -> str:
    q = query.strip()
    if source_id is not None:
        return "source_local"
    if _ID_PATTERN.match(q):
        return "exact"
    return "conceptual"
