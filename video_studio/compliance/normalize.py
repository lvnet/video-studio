"""文本归一化：提高关键词黑名单的绕过成本。

防御目标（合法范围内的对抗）：
- 全角/半角混排（宫崎骏 ／ 宫崎骏）
- 插入空格/标点分隔（宫 崎 骏、宫-崎-骏）
- 繁体/简体混用（宮崎駿）
- 常见同音/形近字替换（皮卡秋 → 皮卡丘）

实现方式：归一化到「比较形式」后再匹配关键词。零外部依赖（内置小映射表）。

诚实声明：这是提高门槛，不是绝对防御。任何关键词方案都能被人为绕过；
真正的零风险仍需内容安全服务 + 人工审核 + 留痕审计（见 COMPLIANCE.md）。
"""

from __future__ import annotations

import re
import unicodedata


# 常见繁体 → 简体映射（覆盖本守卫关键词涉及的字，非完整转换表）
_T2S = {
    "宮": "宫", "崎": "崎", "駿": "骏", "骏": "骏", "新": "新", "誠": "诚",
    "吉": "吉", "卜": "卜", "力": "力", "泥": "泥", "斯": "斯", "尼": "尼",
    "梵": "梵", "高": "高", "畢": "毕", "加索": "加索", "卡": "卡", "丘": "丘",
    "瑪": "玛", "米": "米", "老": "老", "鼠": "鼠", "鋼": "钢", "鐵": "铁",
    "俠": "侠", "蜘": "蜘", "蛛": "蛛", "哈": "哈", "利": "利", "波": "波",
    "特": "特", "冰雪": "冰雪", "緣": "缘", "奧": "奥", "曼": "曼", "哆": "哆",
    "夢": "梦", "風": "风", "格": "格", "樣": "样", "畫": "画", "師": "师",
    "製": "制", "炸": "炸", "彈": "弹", "毒": "毒", "證": "证", "件": "件",
    "血": "血", "腥": "腥", "裸露": "裸露", "仇恨": "仇恨", "deepfake": "deepfake",
}

# 常见形近/同音替换 → 规范字（针对高风险词）
_VARIANT = {
    "秋": "丘",     # 皮卡秋 → 皮卡丘
    "跂": "崎",     # 宫跂骏
    "峻": "骏",     # 宫崎峻
    "成": "诚",     # 新海成
    "伹": "但",     # 通用
}


def _to_simplify(text: str) -> str:
    return "".join(_T2S.get(ch, ch) for ch in text)


def _map_variants(text: str) -> str:
    return "".join(_VARIANT.get(ch, ch) for ch in text)


def normalize(text: str) -> str:
    """把文本转成用于匹配比较的规范形式。

    步骤：NFKC(全角→半角) → 去所有空白与常见分隔符 → 繁体转简体 → 形近字归一 → 小写。
    """
    text = unicodedata.normalize("NFKC", text)
    text = _to_simplify(text)
    text = _map_variants(text)
    # 去掉空白与常见分隔符号，使「宫 崎 骏」「宫-崎-骏」等价于「宫崎骏」
    text = re.sub(r"[\s\-_.·,，。、/\\|]", "", text)
    return text.lower()


def normalize_pattern(pattern: str) -> str:
    """关键词也走同样的归一化，保证两边可比。"""
    return normalize(pattern)


def match_normalized(patterns, text: str):
    """在归一化后的文本上匹配归一化后的关键词。

    patterns: Iterable[(原始关键词, 原因)]。
    yield (原始关键词, 原因)。
    """
    nt = normalize(text)
    for needle, reason in patterns:
        np = normalize_pattern(needle)
        if np and np in nt:
            yield needle, reason
