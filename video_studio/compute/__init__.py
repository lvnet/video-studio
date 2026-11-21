"""算力适配层：统一 Backend 接口、三种后端（默认桩、可切真实）、注册表。

- cloud_api：云端模型 API（默认桩，可注入客户端走真实协议）
- local_comfyui：本地 ComfyUI（全套 HTTP API，默认桩）
- cloud_gpu：云 GPU + Agent 执行（能力匹配/租约/产物推送契约，默认桩）

默认全部走内存桩：零外连、零成本、零版权风险。要真实调用须显式关闭 dry_run
并注入 HttpClient。本层保证接口一致、可插拔、可切换。
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
from .cloud_gpu import CloudGpuBackend
from .local_comfyui import LocalComfyUIBackend
from .registry import BackendRegistry, default_registry
from .config import ComputeConfig
from .http_client import (
    HttpClient, UrllibHttpClient, StubHttpClient, FakeHttpClient,
    Response, HttpError, TransportError,
)
from .comfyui_workflow import build_workflow, outputs_from_history

__all__ = [
    "Artifact", "Backend", "BackendKind", "Cost", "FailureCode", "Progress",
    "GenRequest", "Task",  # Task 为向后兼容别名，新代码用 GenRequest
    "CloudApiBackend", "LocalComfyUIBackend", "CloudGpuBackend",
    "BackendRegistry", "default_registry",
    "ComputeConfig",
    "HttpClient", "UrllibHttpClient", "StubHttpClient", "FakeHttpClient",
    "Response", "HttpError", "TransportError",
    "build_workflow", "outputs_from_history",
]
