"""算力适配层：统一 Backend 接口、三种后端桩、注册表。

- cloud_api：云端模型 API（MVP 首选，真实调用后置）
- local_comfyui：本地 ComfyUI（真实调用后置）
- cloud_gpu：云 GPU + Agent 执行（真实调度后置）

真实外部算力全部后置；本层保证接口一致、可插拔、可切换。
"""

from .base import (
    Artifact,
    Backend,
    BackendKind,
    Cost,
    FailureCode,
    Progress,
    GenRequest,
    Task,
)
from .cloud_api import CloudApiBackend
from .local_comfyui import LocalComfyUIBackend
from .cloud_gpu import CloudGpuBackend
from .registry import BackendRegistry, default_registry

__all__ = [
    "Artifact", "Backend", "BackendKind", "Cost", "FailureCode", "Progress",
    "GenRequest", "Task",  # Task 为向后兼容别名，新代码用 GenRequest
    "CloudApiBackend", "LocalComfyUIBackend", "CloudGpuBackend",
    "BackendRegistry", "default_registry",
]
