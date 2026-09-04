"""哈希规范化表驱动测试（规格 13.1）。"""

import pytest

from learning_wiki.domain import hashing

CASES = [
    # (输入 a, 输入 b, 是否应相等, 说明)
    ("﻿测试\r\n文本", "测试\n文本", True, "BOM + CRLF 归一"),
    ("测试\r\r文本", "测试\n\n文本", True, "孤立 CR 归一为 LF"),
    ("行尾空白  \n下一行", "行尾空白\n下一行", True, "行尾空白去除"),
    ("  首尾空白  ", "首尾空白", True, "首尾空白去除"),
    ("ｆｕｌｌｗｉｄｔｈ", "fullwidth", True, "NFKC 全角归半角"),
    ("测试 文本", "测试  文本", False, "词间空白保留"),
    ("测试\xa0文本", "测试 文本", True, "NFKC 将 NBSP 归一为空格"),
]


@pytest.mark.parametrize(("a", "b", "expect_equal", "why"), CASES)
def test_normalization(a: str, b: str, expect_equal: bool, why: str) -> None:
    equal = hashing.hash_text(a) == hashing.hash_text(b)
    assert equal == expect_equal, why


def test_hash_prefix() -> None:
    assert hashing.hash_text("x").startswith("sha256:")


def test_span_hash_equals_content_hash_semantics() -> None:
    assert hashing.span_hash("同一段文本") == hashing.hash_text("同一段文本")


def test_tamper_detection_by_hash(tmp_path) -> None:  # 规格 13.3.6
    import time

    f = tmp_path / "f.md"
    f.write_text("原始内容\n", encoding="utf-8")
    h1 = hashing.hash_file(f)
    # 篡改内容、伪造普通时间戳
    f.write_text("篡改内容\n", encoding="utf-8")
    import os

    stat = os.stat(f)
    os.utime(f, (stat.st_atime, stat.st_mtime))
    assert hashing.hash_file(f) != h1
    time.sleep(0)  # 时间戳不影响内容哈希
