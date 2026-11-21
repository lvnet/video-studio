"""版权与内容安全守卫。

这是硬性红线的技术抓手：所有进入生成链路的提示词、素材引用、示例内容，
都必须先过本模块。设计原则是「默认拒绝、显式放行」——宁可误拦，不可放过
版权风险。

三类检查：
1. 提示词版权检查：拒绝复刻在世作者的具体画风、受版权保护的具名角色/作品、
   真人换脸等明确高风险表述。
2. 素材来源检查：任何被镜头引用的资产必须声明合法来源，未声明即拒绝。
3. 用途合规检查：拒绝违法违规用途。

注意：本模块是「最小可行守卫」，规则可扩充（见 RULES）。它不是法律意见，
不承诺覆盖所有风险；真正的合规责任由使用者承担（见 COMPLIANCE.md）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class RiskLevel(str, Enum):
    CLEAN = "clean"          # 无风险
    REVIEW = "review"        # 需人工复核（不自动拒绝）
    BLOCK = "block"          # 硬性拒绝


class ViolationKind(str, Enum):
    """违规类型，命名空间化，便于诊断与统计。"""

    LIVING_ARTIST_STYLE = "COPYRIGHT_LIVING_ARTIST_STYLE"  # 复刻在世作者具体风格
    NAMED_IP_CHARACTER = "COPYRIGHT_NAMED_IP_CHARACTER"    # 具名受保护角色/作品
    DEEPFAKE_REAL_PERSON = "CONTENT_DEEPFAKE_REAL_PERSON"  # 真人换脸/伪造
    ILLEGAL_USE = "CONTENT_ILLEGAL_USE"                    # 违法违规用途
    ASSET_PROVENANCE_MISSING = "ASSET_PROVENANCE_MISSING"  # 素材来源未声明
    ASSET_PROVENANCE_INVALID = "ASSET_PROVENANCE_INVALID"  # 素材来源声明无效


@dataclass(frozen=True)
class Finding:
    """单条检查结果。"""

    kind: ViolationKind
    level: RiskLevel
    message: str
    matched: str = ""  # 命中的原文片段（已脱敏展示用）


@dataclass
class GuardReport:
    """一次检查的汇总报告。"""

    subject: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def level(self) -> RiskLevel:
        """取最高风险等级。"""
        if any(f.level == RiskLevel.BLOCK for f in self.findings):
            return RiskLevel.BLOCK
        if any(f.level == RiskLevel.REVIEW for f in self.findings):
            return RiskLevel.REVIEW
        return RiskLevel.CLEAN

    @property
    def passed(self) -> bool:
        """是否放行：只有非 BLOCK 才放行。"""
        return self.level != RiskLevel.BLOCK

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == RiskLevel.BLOCK]


# ---------------------------------------------------------------------------
# 规则集：关键词匹配 + 风险等级。规则可外部扩充，但核心硬红线内置在此。
# 说明：这些是「最小可行」规则，用于拦截最典型、最明确的高风险表述。
# ---------------------------------------------------------------------------

# 复刻在世/近现代作者的具体风格 —— 行业与平台普遍视为高风险。
_LIVING_ARTIST_PATTERNS: tuple[tuple[str, str], ...] = (
    ("宫崎骏风格", "复刻特定作者画风"),
    ("新海诚风格", "复刻特定作者画风"),
    ("吉卜力风格", "复刻特定工作室风格"),
    ("迪士尼风格", "复刻特定工作室风格"),
    ("皮克斯风格", "复刻特定工作室风格"),
    ("梵高风格", "复刻特定作者画风"),
    ("毕加索风格", "复刻特定作者画风"),
    ("in the style of", "复刻特定作者风格(英文)"),
)

# 受版权保护的具名角色/作品 —— 具名即拦，避免直接复刻 IP。
_NAMED_IP_PATTERNS: tuple[tuple[str, str], ...] = (
    ("皮卡丘", "具名IP角色"),
    ("马里奥", "具名IP角色"),
    ("米老鼠", "具名IP角色"),
    ("钢铁侠", "具名IP角色"),
    ("蜘蛛侠", "具名IP角色"),
    ("哈利波特", "具名IP作品"),
    ("冰雪奇缘", "具名IP作品"),
    ("奥特曼", "具名IP角色"),
    ("哆啦A梦", "具名IP角色"),
    ("hello kitty", "具名IP角色"),
)

# 真人换脸/伪造 —— 肖像权与内容安全风险。
_DEEPFAKE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("换成XXX的脸", "真人换脸"),
    ("换脸成", "真人换脸"),
    ("deepfake", "深度伪造"),
    ("换上真人的脸", "真人换脸"),
)

# 违法违规用途 —— 明确禁止。
_ILLEGAL_USE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("制作炸弹", "违法危险物品"),
    ("如何合成毒品", "违法毒品"),
    ("伪造证件", "违法伪造"),
)


def _match(patterns: Iterable[tuple[str, str]], text: str):
    """归一化匹配：抗全角/空格/繁简/形近绕过。

    见 normalize.match_normalized。规则表原样传入，内部对关键词与文本双向归一化。
    """
    from .normalize import match_normalized
    yield from match_normalized(patterns, text)


def check_prompt(prompt: str, subject: str = "prompt") -> GuardReport:
    """检查提示词的版权与内容安全风险。

    复刻在世作者风格、具名IP角色、真人换脸、违法用途 → BLOCK；
    其余 → CLEAN。命中即返回，避免报告过长。
    """
    report = GuardReport(subject=subject)

    for needle, reason in _match(_LIVING_ARTIST_PATTERNS, prompt):
        report.findings.append(Finding(
            kind=ViolationKind.LIVING_ARTIST_STYLE,
            level=RiskLevel.BLOCK,
            message=f"禁止复刻特定作者/工作室风格（{reason}）：{needle}",
            matched=needle,
        ))
    for needle, reason in _match(_NAMED_IP_PATTERNS, prompt):
        report.findings.append(Finding(
            kind=ViolationKind.NAMED_IP_CHARACTER,
            level=RiskLevel.BLOCK,
            message=f"禁止直接复刻受版权保护的具名角色/作品（{reason}）：{needle}",
            matched=needle,
        ))
    for needle, reason in _match(_DEEPFAKE_PATTERNS, prompt):
        report.findings.append(Finding(
            kind=ViolationKind.DEEPFAKE_REAL_PERSON,
            level=RiskLevel.BLOCK,
            message=f"禁止真人换脸/伪造（{reason}）：{needle}",
            matched=needle,
        ))
    for needle, reason in _match(_ILLEGAL_USE_PATTERNS, prompt):
        report.findings.append(Finding(
            kind=ViolationKind.ILLEGAL_USE,
            level=RiskLevel.BLOCK,
            message=f"禁止违法违规用途（{reason}）：{needle}",
            matched=needle,
        ))
    return report


# 素材来源允许值：只有这四类可放行，其余视为无效。
ALLOWED_PROVENANCE = frozenset({
    "self_created",       # 本人原创
    "public_domain",      # 公有领域（需可举证）
    "licensed",           # 已获授权（需保留授权凭证引用）
    "ai_generated",       # 本项目生成
})


@dataclass(frozen=True)
class AssetProvenance:
    """素材来源声明。任何被镜头引用的资产都必须携带。"""

    asset_id: str
    source: str                       # 必须是 ALLOWED_PROVENANCE 之一
    rights_holder: str = ""           # 权利人（原创可填空）
    license_ref: str = ""             # 授权凭证/链接引用（不存凭证本身）
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "source": self.source,
            "rights_holder": self.rights_holder,
            "license_ref": self.license_ref,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AssetProvenance":
        return cls(**{k: data.get(k, "") for k in (
            "asset_id", "source", "rights_holder", "license_ref", "note"
        )})


def check_asset_provenance(provenance: AssetProvenance | None) -> GuardReport:
    """检查素材来源声明。缺失或无效 → BLOCK。"""
    report = GuardReport(subject=provenance.asset_id if provenance else "asset")

    if provenance is None:
        report.findings.append(Finding(
            kind=ViolationKind.ASSET_PROVENANCE_MISSING,
            level=RiskLevel.BLOCK,
            message="素材缺少来源声明，禁止被镜头引用",
        ))
        return report

    if provenance.source not in ALLOWED_PROVENANCE:
        report.findings.append(Finding(
            kind=ViolationKind.ASSET_PROVENANCE_INVALID,
            level=RiskLevel.BLOCK,
            message=(
                f"素材来源声明无效：{provenance.source!r}；"
                f"允许值：{sorted(ALLOWED_PROVENANCE)}"
            ),
        ))
    # licensed / public_domain 建议保留凭证引用，但不强制 BLOCK（自用场景务实处理）。
    elif provenance.source in {"licensed", "public_domain"} and not (
        provenance.license_ref or provenance.rights_holder
    ):
        report.findings.append(Finding(
            kind=ViolationKind.ASSET_PROVENANCE_INVALID,
            level=RiskLevel.REVIEW,
            message=f"来源为 {provenance.source} 但未填写权利人或凭证引用，建议补全以便追溯",
        ))
    return report


def enforce(report: GuardReport) -> None:
    """守卫执行器：BLOCK 时抛异常，用于生成链路强制拦截。"""
    if not report.passed:
        errs = "; ".join(f.message for f in report.errors)
        raise ComplianceBlocked(f"合规检查未通过（{report.subject}）：{errs}")


class ComplianceBlocked(ValueError):
    """合规检查阻断异常。"""
