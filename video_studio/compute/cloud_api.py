"""云端模型 API 后端（MVP 首选：MiniMax H3）。"""

from __future__ import annotations

import hashlib
from typing import Any

from .base import (
    Artifact,
    Backend,
    BackendKind,
    Cost,
    FailureCode,
    Progress,
    GenRequest,
)


class CloudApiBackend(Backend):
    """通过 HTTP 调用云端视频生成 API。

    此模式上传为 no-op：产物已在云端，fetch 返回云端 storage_uri。
    """

    def __init__(self, api_key: str, endpoint: str, model: str = "MiniMax-H3"):
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self._tasks: dict[str, Progress] = {}

    @property
    def kind(self) -> BackendKind:
        return BackendKind.CLOUD_API

    def capabilities(self) -> dict[str, Any]:
        return {"model": self.model, "modality": ["text", "image", "video"]}

    def health(self) -> bool:
        # TODO: 真实健康检查（轻量探活接口）
        return bool(self.api_key)

    def estimate(self, task: GenRequest) -> Cost:
        # TODO: 接入真实计价（按 token / 秒数 / 分辨率）
        return Cost(estimated=0.0, breakdown={"model": self.model})

    def submit(self, task: GenRequest) -> str:
        # TODO: 构造请求并 POST 到 self.endpoint，返回远端 task_id
        task_id = hashlib.sha1(
            (task.step + str(task.payload)).encode("utf-8")
        ).hexdigest()[:16]
        self._tasks[task_id] = Progress(task_id, "queued")
        return task_id

    def poll(self, task_id: str) -> Progress:
        # TODO: 查询远端任务状态并映射
        return self._tasks.get(task_id, Progress(task_id, "unknown"))

    def fetch(self, task_id: str) -> Artifact:
        # TODO: 取云端产物 URI 与 sha256
        raise NotImplementedError("cloud_api.fetch 待接入真实 API")

    def cancel(self, task_id: str) -> bool:
        # TODO: 调用远端取消接口
        return self._tasks.pop(task_id, None) is not None

    def normalize_error(self, provider_error: Any) -> FailureCode:
        text = str(provider_error).lower()
        if any(k in text for k in ("timeout", "429", "rate limit", "temporar")):
            return FailureCode.TRANSIENT
        if any(k in text for k in ("invalid", "400", "parameter")):
            return FailureCode.INVALID_PARAMS
        return FailureCode.UNKNOWN
