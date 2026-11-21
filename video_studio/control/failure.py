"""最小失败码表 + 可重试分类 + 中文诊断（方案 04/核心壁垒之一）。

失败按命名空间归类，便于统计与定位：
  PROVIDER_  算力后端/供应商错误
  PROMPT_    提示词问题
  ASSET_     资产问题（未批准、版本失效等）
  SCHEDULER_ 调度问题（租约、节点等）
  COST_      成本/预算问题

每个失败码带：是否可重试、中文诊断、建议动作。后端原始错误经
normalize 归一到本表，状态机据此决策「重试」还是「进死信」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RetryPolicy(str, Enum):
    RETRY = "retry"          # 可重试
    NO_RETRY = "no_retry"    # 重试无意义
    MANUAL = "manual"        # 需人工介入（进死信）


@dataclass(frozen=True)
class FailureSpec:
    code: str
    namespace: str
    retry: RetryPolicy
    diagnostic: str          # 中文诊断
    action: str              # 建议动作


# 最小失败码表（可扩充）
FAILURE_SPECS: dict[str, FailureSpec] = {
    # PROVIDER_
    "PROVIDER_TRANSIENT": FailureSpec(
        "PROVIDER_TRANSIENT", "PROVIDER_", RetryPolicy.RETRY,
        "供应商临时错误（网络抖动/限流）", "稍后自动重试；频繁出现请检查 API 配额"),
    "PROVIDER_TIMEOUT": FailureSpec(
        "PROVIDER_TIMEOUT", "PROVIDER_", RetryPolicy.RETRY,
        "供应商响应超时", "自动重试；可在后端配置调大超时"),
    "PROVIDER_INVALID_PARAMS": FailureSpec(
        "PROVIDER_INVALID_PARAMS", "PROVIDER_", RetryPolicy.NO_RETRY,
        "提交参数不被供应商接受", "检查分辨率/时长/步数是否超出模型支持范围"),
    "PROVIDER_AUTH": FailureSpec(
        "PROVIDER_AUTH", "PROVIDER_", RetryPolicy.MANUAL,
        "供应商鉴权失败（密钥无效或过期）", "更新 .env 中的 API 密钥后重试"),
    "PROVIDER_NODE_DEAD": FailureSpec(
        "PROVIDER_NODE_DEAD", "PROVIDER_", RetryPolicy.RETRY,
        "执行节点失联", "重新派单到其他节点；检查节点心跳"),

    # PROMPT_
    "PROMPT_SCHEMA": FailureSpec(
        "PROMPT_SCHEMA", "PROMPT_", RetryPolicy.NO_RETRY,
        "提示词不符合权威 Schema", "用规则引擎重新扩写，勿手工改字段"),
    "PROMPT_COMPLIANCE": FailureSpec(
        "PROMPT_COMPLIANCE", "PROMPT_", RetryPolicy.MANUAL,
        "提示词命中版权/内容安全红线", "修改违规表述（如复刻特定风格、具名IP）后再提交"),
    "PROMPT_SPEAKER_BINDING": FailureSpec(
        "PROMPT_SPEAKER_BINDING", "PROMPT_", RetryPolicy.NO_RETRY,
        "说话人缺少 voice_id 绑定", "为对白角色绑定已批准的音色锚点"),

    # ASSET_
    "ASSET_NOT_APPROVED": FailureSpec(
        "ASSET_NOT_APPROVED", "ASSET_", RetryPolicy.MANUAL,
        "引用了未批准的资产", "先在资产库人工批准该资产再引用"),
    "ASSET_VERSION_CONFLICT": FailureSpec(
        "ASSET_VERSION_CONFLICT", "ASSET_", RetryPolicy.NO_RETRY,
        "资产版本与镜头锁定版本不一致", "按镜头锁定的 asset_version 恢复对应版本"),
    "ASSET_PROVENANCE": FailureSpec(
        "ASSET_PROVENANCE", "ASSET_", RetryPolicy.MANUAL,
        "资产来源声明缺失或无效", "补齐素材来源声明（原创/公有领域/已授权/AI生成）"),

    # SCHEDULER_
    "SCHEDULER_NO_BACKEND": FailureSpec(
        "SCHEDULER_NO_BACKEND", "SCHEDULER_", RetryPolicy.MANUAL,
        "没有可用的算力后端", "在后端配置中启用至少一个可用后端"),
    "SCHEDULER_LEASE_LOST": FailureSpec(
        "SCHEDULER_LEASE_LOST", "SCHEDULER_", RetryPolicy.RETRY,
        "任务租约丢失", "重新派单"),
    "SCHEDULER_CAPACITY": FailureSpec(
        "SCHEDULER_CAPACITY", "SCHEDULER_", RetryPolicy.RETRY,
        "并发配额已满", "排队等待空闲配额"),

    # COST_
    "COST_BUDGET_EXCEEDED": FailureSpec(
        "COST_BUDGET_EXCEEDED", "COST_", RetryPolicy.MANUAL,
        "累计成本超过预算上限", "提高项目/日预算，或减少本批次的抽卡数量"),
    "COST_ESTIMATE_FAILED": FailureSpec(
        "COST_ESTIMATE_FAILED", "COST_", RetryPolicy.MANUAL,
        "无法计算成本预估", "检查单价表配置是否覆盖当前模型/单位"),

    # 兜底
    "UNKNOWN": FailureSpec(
        "UNKNOWN", "", RetryPolicy.MANUAL,
        "未分类错误", "查看任务日志定位原始错误"),
}


def spec(code: str) -> FailureSpec:
    return FAILURE_SPECS.get(code, FAILURE_SPECS["UNKNOWN"])


def is_retryable(code: str) -> bool:
    return spec(code).retry == RetryPolicy.RETRY


def diagnose(code: str) -> str:
    s = spec(code)
    return f"[{code}] {s.diagnostic}；建议：{s.action}"


def normalize(provider_error: Exception | str) -> str:
    """把后端原始错误归一到失败码表。

    这里做保守映射：明确的超时/限流/鉴权关键字优先，其余归 UNKNOWN。
    真实后端适配器可覆写自己的 normalize_error（接口已预留）。
    """
    text = str(provider_error).lower()
    if any(k in text for k in ("auth", "unauthorized", "401", "403", "invalid api key")):
        return "PROVIDER_AUTH"
    if any(k in text for k in ("timeout", "timed out", "504")):
        return "PROVIDER_TIMEOUT"
    if any(k in text for k in ("rate limit", "429", "temporar", "503", "retry")):
        return "PROVIDER_TRANSIENT"
    if any(k in text for k in ("invalid param", "400", "unsupported resolution")):
        return "PROVIDER_INVALID_PARAMS"
    if any(k in text for k in ("node dead", "offline", "heartbeat")):
        return "PROVIDER_NODE_DEAD"
    return "UNKNOWN"
