"""算力适配层：统一 Backend 接口与三种后端实现。

- cloud_api：云端模型 API（MVP 首选）
- local_comfyui：本地 ComfyUI
- cloud_gpu：云 GPU + Agent 执行（待实现）
"""

from .base import (
    Artifact,
    Backend,
    BackendKind,
    Cost,
    FailureCode,
    Progress,
    Task,
)
from .cloud_api import CloudApiBackend
from .local_comfyui import LocalComfyUIBackend

__all__ = [
    "Artifact",
    "Backend",
    "BackendKind",
    "Cost",
    "FailureCode",
    "Progress",
    "Task",
    "CloudApiBackend",
    "LocalComfyUIBackend",
]
