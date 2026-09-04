"""unified diff 的生成与严格应用。

设计取舍：第三方 patch 库面向文件操作且上下文不匹配时静默跳过，
不符合「失败即冲突」的要求（规格 5.7 base_hash/result_hash 的确定性），
因此自实现字符串级应用器：

- ``make_diff``：difflib 生成（行尾换行保留）；
- ``apply_unified``：严格应用——上下文或删除行与原文不匹配立即抛
  ``PatchConflictError``，绝不模糊重试、绝不静默跳过。
"""

from __future__ import annotations

import difflib

_HUNK_HEADER = "@@ -{old_start},{old_count} +{new_start},{new_count} @@"


class PatchConflictError(Exception):
    """diff 与原文不匹配（上下文漂移 / base_hash 过期）。"""


def _ensure_trailing_newline(text: str) -> str:
    """提案管道统一补齐尾换行。

    无尾换行的行在 diff 中无法与后续行区分（'-b'+'+B' 拼接歧义），
    因此 make_diff / apply_unified 都先规范化；对 Markdown 内容无害。
    """
    return text if text.endswith("\n") or text == "" else text + "\n"


def make_diff(base: str, after: str, path: str) -> str:
    """生成 unified diff（from/to 文件名统一用目标路径，n=3 上下文）。"""
    diff = difflib.unified_diff(
        _ensure_trailing_newline(base).splitlines(keepends=True),
        _ensure_trailing_newline(after).splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(diff)


def _parse_hunk_header(line: str) -> tuple[int, int]:
    """'@@ -l,s +l,s @@' → (old_start, old_count)；s 缺省为 1。"""
    inner = line.strip().strip("@").strip()
    old_part = inner.split(" ")[0]  # -l,s
    body = old_part.lstrip("-")
    if "," in body:
        start, count = body.split(",")
        return int(start), int(count)
    return int(body), 1


def apply_unified(base: str, diff: str) -> str:
    """严格应用 unified diff 到 base；任何不匹配抛 PatchConflictError。"""
    base_lines = _ensure_trailing_newline(base).splitlines(keepends=True)
    diff_lines = diff.splitlines(keepends=True)

    result: list[str] = []
    base_pos = 0  # 已消费到 base_lines 的位置（0-based）

    i = 0
    while i < len(diff_lines):
        line = diff_lines[i]
        if line.startswith(("--- ", "+++ ")):
            i += 1
            continue
        if line.startswith("@@"):
            old_start, old_count = _parse_hunk_header(line)
            # hunk 按 old 位置顺序出现：把未触及的原文行原样复制
            hunk_base_start = old_start - 1 if old_start > 0 else 0
            if hunk_base_start < base_pos:
                raise PatchConflictError("hunk 位置回退（diff 损坏）")
            result.extend(base_lines[base_pos:hunk_base_start])
            base_pos = hunk_base_start
            i += 1
            consumed = 0
            while i < len(diff_lines) and not diff_lines[i].startswith(("@@", "--- ", "+++ ")):
                dline = diff_lines[i]
                marker, content = dline[0], dline[1:]
                if marker == " ":
                    if consumed >= old_count or base_lines[base_pos] != content:
                        raise PatchConflictError(f"上下文不匹配: {dline!r}")
                    result.append(base_lines[base_pos])
                    base_pos += 1
                    consumed += 1
                elif marker == "-":
                    if consumed >= old_count or base_lines[base_pos] != content:
                        raise PatchConflictError(f"删除行不匹配: {dline!r}")
                    base_pos += 1
                    consumed += 1
                elif marker == "+":
                    result.append(content)
                elif line == "\n" or dline.strip() == "":
                    # 空 diff 行（EOF 无换行等场景）按上下文处理
                    if consumed < old_count and base_lines[base_pos] == dline:
                        result.append(base_lines[base_pos])
                        base_pos += 1
                        consumed += 1
                else:
                    raise PatchConflictError(f"无法识别的 diff 行: {dline!r}")
                i += 1
            if consumed != old_count:
                raise PatchConflictError(f"hunk 行数不匹配（消费 {consumed}，声明 {old_count}）")
        else:
            # 没有先出现 @@ 头的 diff 内容
            raise PatchConflictError(f"diff 头缺失: {line!r}")

    result.extend(base_lines[base_pos:])
    return "".join(result)
