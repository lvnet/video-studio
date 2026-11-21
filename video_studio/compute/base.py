"""算力适配层统一接口。

创作层与控制面只依赖 Backend 抽象，不关心底层跑在本地显卡、云 API 还是云 GPU。
新增后端只需实现本接口，不改上层业务。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BackendKind(str, Enum):
    CLOUD_API = "cloud_api"        # 云端模型 API（如 MiniMax H3）
    LOCAL_COMFYUI = "local_comfyui"  # 本地 ComfyUI
    CLOUD_GPU = "cloud_gpu"        # 云 GPU + Agent 执行


@dataclass
class Cost:
    """提交前成本预估。三段口径：预估 / 实际 / 上限。"""

    estimated: float = 0.0
    currency: str = "CNY"
    breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class Progress:
    task_id: str
    state: str
    progress: float = 0.0
    message: str = ""


@dataclass
class Artifact:
    """产物契约：必须含规范化 storage_uri 与 sha256。

    API 模式上传为 no-op；本地/云 GPU 由 Worker/Agent 主动推送对象存储
    （控制面无法拉取 NAT 后的文件）。
    """

    storage_uri: str
    sha256: str
    media_type: str = ""
    size_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class FailureCode(str, Enum):
    """最小失败码表。后端原始错误统一归一化到此类，供状态机决策重试/死信。"""

    TRANSIENT = "transient"          # 可重试：网络抖动、限流
    INVALID_PARAMS = "invalid_params"  # 参数错误，重试无意义
    OUT_OF_BUDGET = "out_of_budget"    # 预算上限
    NODE_DEAD = "node_dead"            # 节点失联
    UNKNOWN = "unknown"


@dataclass
class GenRequest:
    """算力层的一次生成请求。

    注意与 models.Task（持久化的任务实体）区分：GenRequest 是「发给后端的请求」，
    Task 是「控制面跟踪的任务记录」。历史上本类曾命名为 Task，保留别名兼容。
    """

    task_id: str
    step: str
    payload: dict[str, Any]
    budget_limit: float | None = None


# 向后兼容别名：老代码 from .base import Task 仍可用（指向 GenRequest）。
# 新代码请用 GenRequest，避免与 models.Task 混淆。
Task = GenRequest


class Backend(ABC):
    """所有算力后端实现的统一接口。"""

    @property
    @abstractmethod
    def kind(self) -> BackendKind:
        ...

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """后端能力声明（支持的模型、分辨率、并发等）。"""

    @abstractmethod
    def health(self) -> bool:
        """健康检查。"""

    @abstractmethod
    def estimate(self, task: GenRequest) -> Cost:
        """提交前成本预估。"""

    @abstractmethod
    def submit(self, task: GenRequest) -> str:
        """提交任务，返回 task_id。"""

    @abstractmethod
    def poll(self, task_id: str) -> Progress:
        """轮询任务进度。"""

    @abstractmethod
    def fetch(self, task_id: str) -> Artifact:
        """取回产物。"""

    @abstractmethod
    def cancel(self, task_id: str) -> bool:
        """取消任务。"""

    @abstractmethod
    def normalize_error(self, provider_error: Any) -> FailureCode:
        """把后端原始错误归一化成统一失败码。"""
