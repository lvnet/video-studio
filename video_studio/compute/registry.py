"""算力后端注册表：按 kind 注册/选择/切换（方案愿景的核心抽象）。

上层业务只依赖 Backend 接口，通过注册表选择后端；切换后端不改剧本、提示词、
项目数据。真实后端实现全部后置，注册表保证「接口一致、可插拔」这一层先跑通。
"""

from __future__ import annotations

from typing import Callable

from .base import Backend, BackendKind


class BackendRegistry:
    """kind → 后端工厂/实例的注册表。"""

    def __init__(self) -> None:
        self._factories: dict[BackendKind, Callable[[], Backend]] = {}
        self._instances: dict[BackendKind, Backend] = {}

    def register(self, kind: BackendKind | str, factory: Callable[[], Backend]) -> None:
        k = BackendKind(kind) if isinstance(kind, str) else kind
        self._factories[k] = factory
        self._instances.pop(k, None)  # 工厂变更，清掉旧实例

    def get(self, kind: BackendKind | str) -> Backend:
        k = BackendKind(kind) if isinstance(kind, str) else kind
        if k in self._instances:
            return self._instances[k]
        if k not in self._factories:
            raise KeyError(f"未注册算力后端：{k.value}")
        inst = self._factories[k]()
        self._instances[k] = inst
        return inst

    def available(self) -> list[BackendKind]:
        return list(self._factories.keys())

    def switch(self, task_backend_id: str, kind: BackendKind | str) -> Backend:
        """切换后端：返回目标后端实例。上层数据不变，只是换执行后端。"""
        return self.get(kind)

    def clear(self) -> None:
        self._factories.clear()
        self._instances.clear()


def default_registry() -> BackendRegistry:
    """默认注册表：装载三种桩后端（真实实现后置）。"""
    from .cloud_api import CloudApiBackend
    from .cloud_gpu import CloudGpuBackend
    from .local_comfyui import LocalComfyUIBackend

    reg = BackendRegistry()
    reg.register(BackendKind.CLOUD_API, lambda: CloudApiBackend("stub-key", "http://stub"))
    reg.register(BackendKind.LOCAL_COMFYUI, LocalComfyUIBackend)
    reg.register(BackendKind.CLOUD_GPU, CloudGpuBackend)
    return reg
