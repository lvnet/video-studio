"""版权与内容安全合规模块。

硬性要求：零版权风险。所有进入生成链路的内容先过本模块。

- guard：版权守卫（提示词 / 素材来源）
- normalize：归一化抗绕过
- content_safety：内容安全分级过滤
- audit：只追加审计日志（哈希链防篡改）
"""

from .guard import (
    ALLOWED_PROVENANCE,
    AssetProvenance,
    ComplianceBlocked,
    Finding,
    GuardReport,
    RiskLevel,
    ViolationKind,
    check_asset_provenance,
    check_prompt,
    enforce,
)
from .normalize import normalize, normalize_pattern
from .content_safety import (
    ContentCategory,
    ContentRule,
    ContentSafetyFilter,
    screen_content,
)
from .audit import AuditEvent, AuditLog

__all__ = [
    "ALLOWED_PROVENANCE",
    "AssetProvenance",
    "ComplianceBlocked",
    "Finding",
    "GuardReport",
    "RiskLevel",
    "ViolationKind",
    "check_asset_provenance",
    "check_prompt",
    "enforce",
    "normalize",
    "normalize_pattern",
    "ContentCategory",
    "ContentRule",
    "ContentSafetyFilter",
    "screen_content",
    "AuditEvent",
    "AuditLog",
]
