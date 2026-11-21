"""核心数据模型（方案 05 DATA MODEL）。

设计口径：
- 所有实体用 dataclass 表达，字段与方案表一一对应。
- 生成物只存对象存储地址或本地相对路径（url / storage_uri），不存二进制进数据。
- 所有生成物带 version 与 parent_id，支撑版本链与复现。
- 采用版唯一真源：outputs.is_adopted；shots.adopted_task_id 为冗余同步字段。
- 资产引用以 task_assets.asset_version 锁定，避免旧镜头重生成时变脸。

本模块不依赖任何数据库或外部服务；持久化交给 store.py（本地 JSON）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """无需外部依赖的短 ID（时间 + 随机）。"""
    import secrets
    import time
    return f"{prefix}_{int(time.time() * 1000):x}_{secrets.token_hex(3)}"


class EntityMixin:
    """提供序列化/反序列化，支持嵌套 dataclass 与 dict/list 原样保留。"""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[arg-type]

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        return cls(**data)


@dataclass
class Project(EntityMixin):
    id: str
    name: str
    aspect_ratio: str = "9:16"          # 9:16 / 16:9
    style_preset: str = "realistic_3d"  # 风格预设
    cover_url: str = ""
    status: str = "active"
    budget_total: float = 0.0           # 项目总预算（元）；0 表示不限
    budget_daily: float = 0.0
    created_at: str = field(default_factory=utcnow)


@dataclass
class Episode(EntityMixin):
    id: str
    project_id: str
    index: int
    title: str = ""
    script_text: str = ""
    summary: str = ""                   # 前情摘要，供续写引用
    duration_target: float = 60.0       # 目标单集时长（秒）


@dataclass
class Asset(EntityMixin):
    """角色锚点图与状态表。approved 是人工批准硬门槛。"""

    id: str
    project_id: str
    type: str                           # role / scene / storyboard
    name: str
    appearance_json: dict[str, Any] = field(default_factory=dict)
    voice_id: str = ""
    approved: bool = False
    image_url: str = ""
    version: int = 1
    provenance: dict[str, Any] = field(default_factory=dict)  # AssetProvenance.to_dict()
    created_at: str = field(default_factory=utcnow)


@dataclass
class TaskAsset(EntityMixin):
    """任务与镜头引用的参考图/音色锚点绑定，锁定版本以复现一致性。"""

    id: str
    task_id: str
    asset_id: str
    asset_version: int
    is_primary: bool = False


@dataclass
class Shot(EntityMixin):
    """镜头是生成任务最小单位。"""

    id: str
    episode_id: str
    index: int
    location: str = ""
    action: str = ""
    dialogue: str = ""
    prompt_text: str = ""
    duration: float = 5.0
    asset_refs: list[str] = field(default_factory=list)  # asset_id 列表
    asset_version: int = 1                              # 锁定的参考资产版本
    adopted_task_id: str = ""                           # 冗余真源，同步自 outputs


@dataclass
class RuleSet(EntityMixin):
    """权威 H3 提示词 Schema 与规则集，支撑版本化与热加载。"""

    id: str
    name: str
    version: int
    schema_json: dict[str, Any] = field(default_factory=dict)
    is_default: bool = False


@dataclass
class Prompt(EntityMixin):
    """提示词版本链：parent_id 指向上一版。"""

    id: str
    shot_id: str
    version: int
    content: str
    rule_set_id: str = ""
    parent_id: str = ""
    created_by: str = "user"
    created_at: str = field(default_factory=utcnow)


@dataclass
class BackendConfig(EntityMixin):
    """算力适配层可插拔后端配置；密钥只存引用，不明文。"""

    id: str
    type: str                           # api / local_gpu / cloud_gpu
    endpoint: str = ""
    model: str = ""
    credentials_ref: str = ""           # 环境变量名 / 密钥管理引用，绝不存密钥本身
    priority: int = 0


@dataclass
class PriceRule(EntityMixin):
    """可配置单价，避免硬编码刊例价失真。"""

    id: str
    backend_type: str
    model: str
    unit: str                           # second / token / image
    unit_price_cents: int               # 每单位价格（分）
    effective_at: str = field(default_factory=utcnow)


@dataclass
class TaskEvent(EntityMixin):
    """进度与失败诊断的时序日志。所有状态迁移都写这里。"""

    id: str
    task_id: str
    ts: str
    stage: str                          # 状态名 / 阶段名
    message: str = ""
    payload_json: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task(EntityMixin):
    """任务主表；三段成本对应预估/预留/实际。"""

    id: str
    shot_id: str
    kind: str                           # t2v / i2v / r2v / etc.
    mode: str = "t2v"
    backend_id: str = ""
    params_json: dict[str, Any] = field(default_factory=dict)
    status: str = "created"
    attempt: int = 0
    failure_code: str = ""
    cost_estimate_cents: int = 0
    cost_reserved_cents: int = 0
    cost_actual_cents: int = 0
    idempotency_key: str = ""
    created_at: str = field(default_factory=utcnow)


@dataclass
class Output(EntityMixin):
    """生成结果与选中状态。is_adopted 是采用版唯一真源。"""

    id: str
    task_id: str
    url: str                            # 对象存储地址或本地相对路径
    duration: float = 0.0
    resolution: str = ""
    is_adopted: bool = False
    sha256: str = ""
    version: int = 1
    parent_id: str = ""
    created_at: str = field(default_factory=utcnow)


@dataclass
class CostLedger(EntityMixin):
    """累计成本账本：按项目维度跟踪已花与在途冻结。"""

    id: str                             # 与 project_id 相同
    spent_cents: int = 0
    reserved_cents: int = 0
    updated_at: str = field(default_factory=utcnow)


# 便于按名称取实体类型（store 用）
ENTITY_TYPES: dict[str, type] = {
    "projects": Project,
    "episodes": Episode,
    "assets": Asset,
    "task_assets": TaskAsset,
    "shots": Shot,
    "rule_sets": RuleSet,
    "prompts": Prompt,
    "backend_configs": BackendConfig,
    "price_rules": PriceRule,
    "task_events": TaskEvent,
    "tasks": Task,
    "outputs": Output,
    "cost_ledgers": CostLedger,
}
