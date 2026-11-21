"""本地 ComfyUI 后端。

控制面进程内运行适配器；产物由本地 Worker 主动推送对象存储。
"""

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


class LocalComfyUIBackend(Backend):
    def __init__(self, base_url: str = "http://127.0.0.1:8188"):
        self.base_url = base_url
        self._tasks: dict[str, Progress] = {}

    @property
    def kind(self) -> BackendKind:
        return BackendKind.LOCAL_COMFYUI

    def capabilities(self) -> dict[str, Any]:
        # TODO: 从 /object_info 拉取可用节点与模型
        return {"base_url": self.base_url}

    def health(self) -> bool:
        # TODO: GET /system_stats
        return False

    def estimate(self, task: GenRequest) -> Cost:
        # 本地算力：按预计耗时折算，无直接现金成本
        return Cost(estimated=0.0, currency="CNY", breakdown={"mode": "local"})

    def submit(self, task: GenRequest) -> str:
        # TODO: POST /prompt，拿到 prompt_id
        task_id = hashlib.sha1(
            ("local" + str(task.payload)).encode("utf-8")
        ).hexdigest()[:16]
        self._tasks[task_id] = Progress(task_id, "queued")
        return task_id

    def poll(self, task_id: str) -> Progress:
        # TODO: GET /history/{prompt_id}
        return self._tasks.get(task_id, Progress(task_id, "unknown"))

    def fetch(self, task_id: str) -> Artifact:
        # TODO: 从输出目录取文件 → 推对象存储 → 返回 storage_uri + sha256
        raise NotImplementedError("local_comfyui.fetch 待实现产物上传")

    def cancel(self, task_id: str) -> bool:
        # TODO: POST /interrupt
        return self._tasks.pop(task_id, None) is not None

    def normalize_error(self, provider_error: Any) -> FailureCode:
        text = str(provider_error).lower()
        if "node" in text and ("dead" in text or "offline" in text):
            return FailureCode.NODE_DEAD
        if "oom" in text or "out of memory" in text:
            return FailureCode.TRANSIENT
        return FailureCode.UNKNOWN
