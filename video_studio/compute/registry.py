"""算力后端注册表：按 kind 注册/选择/切换（方案愿景的核心抽象）。

上层业务只依赖 Backend 接口，通过注册表选择后端；切换后端不改剧本、提示词、
项目数据。注册表保证「接口一致、可插拔、可切换」，并支持：
- 从 BackendConfig 实体/配置驱动构造实例（密钥只存引用）
- dry_run 开关透传（默认桩，零外连）
- 记录当前激活后端（active），供工作台展示与切换
"""

from __future__ import annotations

from typing import Any, Callable

from .base import Backend, BackendKind
from .config import ComputeConfig


class BackendRegistry:
    """kind → 后端工厂/实例的注册表，带激活后端状态。"""

    def __init__(self) -> None:
        self._factories: dict[BackendKind, Callable[[], Backend]] = {}
        self._instances: dict[BackendKind, Backend] = {}
        self._configs: dict[BackendKind, dict[str, Any]] = {}
        self._active: BackendKind | None = None

    @staticmethod
    def _coerce(kind: BackendKind | str) -> BackendKind:
        return BackendKind(kind) if isinstance(kind, str) else kind

    def register(self, kind: BackendKind | str, factory: Callable[[], Backend],
                 config: dict[str, Any] | None = None) -> None:
        k = self._coerce(kind)
        self._factories[k] = factory
        self._instances.pop(k, None)  # 工厂变更，清掉旧实例
        if config is not None:
            self._configs[k] = dict(config)
        if self._active is None:
            self._active = k

    def register_config(self, kind: BackendKind | str, cfg: Any) -> Backend:
        """从 BackendConfig 实体（或 dict）驱动注册并构造实例。

        密钥不落库：cfg 里只有 credentials_ref，运行配置在进程内解析。
        返回构造出的实例，便于调用方注入 client。
        """
        k = self._coerce(kind)
        config = ComputeConfig.from_backend_config(cfg)
        data = config.to_public_dict()
        self._configs[k] = data

        def factory() -> Backend:
            if k == BackendKind.CLOUD_API:
                from .cloud_api import CloudApiBackend
                return CloudApiBackend(config=config)
            if k == BackendKind.LOCAL_COMFYUI:
                from .local_comfyui import LocalComfyUIBackend
                return LocalComfyUIBackend(config=config)
            if k == BackendKind.CLOUD_GPU:
                from .cloud_gpu import CloudGpuBackend
                return CloudGpuBackend(config=config)
            raise KeyError(f"未支持的配置驱动后端：{k.value}")

        self.register(k, factory, config=data)
        return self.get(k)

    def get(self, kind: BackendKind | str) -> Backend:
        k = self._coerce(kind)
        if k in self._instances:
            return self._instances[k]
        if k not in self._factories:
            raise KeyError(f"未注册算力后端：{k.value}")
        inst = self._factories[k]()
        self._instances[k] = inst
        return inst

    def available(self) -> list[BackendKind]:
        return list(self._factories.keys())

    def configs(self) -> dict[str, dict[str, Any]]:
        """安全配置视图（不含密钥明文），供工作台展示。"""
        return {k.value: dict(v) for k, v in self._configs.items()
                if k in self._factories}

    @property
    def active(self) -> BackendKind | None:
        return self._active

    def set_active(self, kind: BackendKind | str) -> Backend:
        k = self._coerce(kind)
        backend = self.get(k)  # 未注册会抛 KeyError
        self._active = k
        return backend

    def switch(self, task_backend_id: str, kind: BackendKind | str) -> Backend:
        """切换后端：返回目标后端实例并记为激活。上层数据不变。"""
        return self.set_active(kind)

    def status(self) -> dict[str, Any]:
        """注册表全景：后端列表、能力、健康、配置、激活项。"""
        rows = []
        for k in self._factories:
            b = self.get(k)
            try:
                healthy = b.health()
            except Exception:
                healthy = False
            # 配置优先取注册表登记的安全视图；未登记则从实例自身配置兜底
            cfg = self._configs.get(k)
            if not cfg and hasattr(b, "config"):
                cfg = b.config.to_public_dict()
                cfg["kind"] = k.value
            rows.append({
                "kind": k.value,
                "name": k.value,
                "active": k == self._active,
                "healthy": healthy,
                "capabilities": b.capabilities(),
                "config": cfg or {},
            })
        return {
            "available": [k.value for k in self._factories],
            "active": self._active.value if self._active else "",
            "backends": rows,
        }

    def clear(self) -> None:
        self._factories.clear()
        self._instances.clear()
        self._configs.clear()
        self._active = None


def default_registry() -> BackendRegistry:
    """默认注册表：装载三种后端（默认桩，接口一致、可插拔）。"""
    from .cloud_api import CloudApiBackend
    from .cloud_gpu import CloudGpuBackend
    from .local_comfyui import LocalComfyUIBackend

    reg = BackendRegistry()
    reg.register(BackendKind.CLOUD_API, lambda: CloudApiBackend())
    reg.register(BackendKind.LOCAL_COMFYUI, LocalComfyUIBackend)
    reg.register(BackendKind.CLOUD_GPU, CloudGpuBackend)
    return reg
