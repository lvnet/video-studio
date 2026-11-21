"""云 GPU + Agent 后端（桩，真实算力后置）。

方案中的 C 模式：控制在控制面、执行在 Agent，需要公网可达的 Agent 主动外连。
按米老板要求，外部算力全部放到最后；这里只保留接口一致性实现与明确 TODO，
不接任何真实 GPU 调度。

未来实现要点（来自方案）：
- 节点心跳与能力标签 {model, mode, vram, upscale}
- Agent 主动外连，控制面不拉取 NAT 后文件
- 产物由 Agent 推对象存储，返回规范化 storage_uri + sha256
"""

from __future__ import annotations

import hashlib
from typing import Any

from .base import Artifact, Backend, BackendKind, Cost, FailureCode, Progress, GenRequest


class CloudGpuBackend(Backend):
    """云 GPU 后端桩实现。"""

    def __init__(self, control_endpoint: str = ""):
        self.control_endpoint = control_endpoint
        self._tasks: dict[str, Progress] = {}

    @property
    def kind(self) -> BackendKind:
        return BackendKind.CLOUD_GPU

    def capabilities(self) -> dict[str, Any]:
        # TODO: 从注册节点汇总能力标签
        return {"mode": "cloud_gpu", "status": "stub"}

    def health(self) -> bool:
        # TODO: 探测控制面与 Agent 心跳
        return False

    def estimate(self, task: GenRequest) -> Cost:
        # TODO: 按 GPU 机时 + 卡型计价，设上限
        return Cost(estimated=0.0, currency="CNY", breakdown={"mode": "cloud_gpu_stub"})

    def submit(self, task: GenRequest) -> str:
        # TODO: 按能力匹配节点、下发 Agent、登记租约
        task_id = hashlib.sha1(
            ("gpu" + str(task.payload)).encode("utf-8")
        ).hexdigest()[:16]
        self._tasks[task_id] = Progress(task_id, "queued")
        return task_id

    def poll(self, task_id: str) -> Progress:
        # TODO: 查询 Agent 回报状态
        return self._tasks.get(task_id, Progress(task_id, "unknown"))

    def fetch(self, task_id: str) -> Artifact:
        # TODO: 取 Agent 推送到对象存储的产物 URI + sha256
        raise NotImplementedError("cloud_gpu.fetch 待实现（外部算力后置）")

    def cancel(self, task_id: str) -> bool:
        # TODO: 通知 Agent 取消，释放租约
        return self._tasks.pop(task_id, None) is not None

    def normalize_error(self, provider_error: Any) -> FailureCode:
        text = str(provider_error).lower()
        if any(k in text for k in ("node dead", "offline", "heartbeat")):
            return FailureCode.NODE_DEAD
        if any(k in text for k in ("timeout", "429", "rate limit")):
            return FailureCode.TRANSIENT
        return FailureCode.UNKNOWN
