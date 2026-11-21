"""内容安全过滤器（生成输入/输出的违规分级）。

版权守卫管「版权风险」，本模块管「内容安全风险」：色情低俗、暴恐、违法犯罪、
人身伤害等。分两级：
- BLOCK：明确违规，硬性拒绝。
- REVIEW：疑似/边缘，进入人工复核队列（不自动放行，也不自动拒绝）。

这是本地最小规则守卫，零依赖。真实生产应接供应商内容安全 API（方案里的内容过滤
与抽检），本模块提供统一接口与本地兜底，future 可注入云端过滤器。

注意：规则表刻意保持通用、聚焦行为与用途，不含任何具体敏感人物名单；
使用方应按自身合规要求与当地法规扩充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .guard import Finding, GuardReport, RiskLevel, ViolationKind
from .normalize import match_normalized


class ContentCategory(str, Enum):
    VIOLENCE = "violence"        # 暴恐/人身伤害
    SEXUAL = "sexual"            # 色情低俗
    ILLEGAL = "illegal"          # 违法犯罪
    SELF_HARM = "self_harm"      # 自残自杀
    HARASSMENT = "harassment"    # 骚扰/仇恨


@dataclass(frozen=True)
class ContentRule:
    pattern: str
    category: ContentCategory
    level: RiskLevel


# 明确违规 → BLOCK
_BLOCK_RULES: tuple[ContentRule, ...] = (
    ContentRule("制作炸弹", ContentCategory.ILLEGAL, RiskLevel.BLOCK),
    ContentRule("合成毒品", ContentCategory.ILLEGAL, RiskLevel.BLOCK),
    ContentRule("伪造证件", ContentCategory.ILLEGAL, RiskLevel.BLOCK),
    ContentRule("教我杀人", ContentCategory.VIOLENCE, RiskLevel.BLOCK),
    ContentRule("儿童色情", ContentCategory.SEXUAL, RiskLevel.BLOCK),
    ContentRule("how to make a bomb", ContentCategory.ILLEGAL, RiskLevel.BLOCK),
)

# 疑似/边缘 → REVIEW（进人工复核，不自动放行）
_REVIEW_RULES: tuple[ContentRule, ...] = (
    ContentRule("血腥", ContentCategory.VIOLENCE, RiskLevel.REVIEW),
    ContentRule("自残", ContentCategory.SELF_HARM, RiskLevel.REVIEW),
    ContentRule("自杀", ContentCategory.SELF_HARM, RiskLevel.REVIEW),
    ContentRule("仇恨", ContentCategory.HARASSMENT, RiskLevel.REVIEW),
    ContentRule("裸露", ContentCategory.SEXUAL, RiskLevel.REVIEW),
    ContentRule("gore", ContentCategory.VIOLENCE, RiskLevel.REVIEW),
)


class ContentSafetyFilter:
    """内容安全过滤器。可注入额外的规则（如从配置加载）。"""

    def __init__(self, block_rules=None, review_rules=None):
        self.block_rules: tuple[ContentRule, ...] = block_rules or _BLOCK_RULES
        self.review_rules: tuple[ContentRule, ...] = review_rules or _REVIEW_RULES

    def screen(self, text: str, subject: str = "content") -> GuardReport:
        """对文本做内容安全分级。返回 GuardReport，level 取最高风险。"""
        report = GuardReport(subject=subject)
        pairs_block = [(r.pattern, r) for r in self.block_rules]
        pairs_review = [(r.pattern, r) for r in self.review_rules]

        for needle, rule in match_normalized(pairs_block, text):
            report.findings.append(Finding(
                kind=ViolationKind.ILLEGAL_USE,
                level=RiskLevel.BLOCK,
                message=f"内容安全拦截（{rule.category.value}）：{needle}",
                matched=needle,
            ))
        for needle, rule in match_normalized(pairs_review, text):
            report.findings.append(Finding(
                kind=ViolationKind.ILLEGAL_USE,
                level=RiskLevel.REVIEW,
                message=f"内容安全需人工复核（{rule.category.value}）：{needle}",
                matched=needle,
            ))
        return report


_default_filter = ContentSafetyFilter()


def screen_content(text: str, subject: str = "content") -> GuardReport:
    """便捷函数：用默认过滤器检查内容安全。"""
    return _default_filter.screen(text, subject)
