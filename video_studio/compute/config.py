"""算力后端配置：从环境变量 / BackendConfig 读取，密钥只存引用。

安全约束：
- 密钥不明文入库、绝不下发浏览器：这里只读 ``credentials_ref`` 指向的
  环境变量名，真正取值在进程内完成，配置实体里永远只有变量名。
- 默认 ``dry_run=True``：不显式开启就走内存桩，零外连、零成本。
- 不下载 / 不打包任何模型权重（ComfyUI 与 H3/SeedVR2 由用户自行部署）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


@dataclass
class ComputeConfig:
    """单个算力后端的运行配置。"""

    kind: str = ""                      # cloud_api / local_comfyui / cloud_gpu
    base_url: str = ""                  # 服务端点（ComfyUI / 云端 API / 控制面）
    model: str = ""                     # 模型标识
    credentials_ref: str = ""           # 环境变量名，不是密钥本身
    dry_run: bool = True                # 默认桩，不外连
    timeout: float = 10.0               # 单次请求超时
    poll_interval: float = 1.0          # 轮询间隔（桩模式下用于估算）
    output_dir: str = ""                # 本地产物落盘目录（ComfyUI fetch 用）
    extra: dict[str, Any] = field(default_factory=dict)

    def credential(self) -> str:
        """按引用读取密钥；未设置环境变量返回空串（而非泄露引用名）。"""
        if not self.credentials_ref:
            return ""
        return os.environ.get(self.credentials_ref, "")

    @property
    def has_credential(self) -> bool:
        return bool(self.credential())

    def to_public_dict(self) -> dict[str, Any]:
        """可下发浏览器/可入库的安全视图：不含密钥明文，只含引用名。"""
        return {
            "kind": self.kind,
            "base_url": self.base_url,
            "model": self.model,
            "credentials_ref": self.credentials_ref,
            "credential_present": self.has_credential,
            "dry_run": self.dry_run,
            "timeout": self.timeout,
            "poll_interval": self.poll_interval,
            "output_dir": self.output_dir,
        }

    @classmethod
    def from_backend_config(cls, cfg: Any) -> "ComputeConfig":
        """从持久化 BackendConfig 实体（或 dict）构造运行配置。

        环境变量可覆盖：
        - VS_COMPUTE_DRY_RUN    全局强制桩/实（1/0）
        - VS_COMPUTE_TIMEOUT    全局超时
        - VS_COMPUTE_POLL       全局轮询间隔
        具体后端的 base_url / credentials_ref / model 来自实体。
        """
        if isinstance(cfg, dict):
            type_ = cfg.get("type", cfg.get("kind", ""))
            endpoint = cfg.get("endpoint", "")
            model = cfg.get("model", "")
            ref = cfg.get("credentials_ref", "")
            extra = dict(cfg.get("extra", {}))
        else:
            type_ = getattr(cfg, "type", "")
            endpoint = getattr(cfg, "endpoint", "")
            model = getattr(cfg, "model", "")
            ref = getattr(cfg, "credentials_ref", "")
            extra = dict(getattr(cfg, "extra", {}) or {})

        # 默认仍为桩；只有显式 VS_COMPUTE_DRY_RUN=0 才尝试真实连接
        dry = _env_bool("VS_COMPUTE_DRY_RUN", True)
        return cls(
            kind=type_,
            base_url=endpoint or os.environ.get(_endpoint_env(type_), ""),
            model=model,
            credentials_ref=ref,
            dry_run=dry,
            timeout=_env_float("VS_COMPUTE_TIMEOUT", 10.0),
            poll_interval=_env_float("VS_COMPUTE_POLL", 1.0),
            output_dir=extra.get("output_dir", os.environ.get("VS_COMPUTE_OUTPUT", "")),
            extra=extra,
        )


def _endpoint_env(kind: str) -> str:
    return {
        "cloud_api": "VS_CLOUD_API_ENDPOINT",
        "local_comfyui": "VS_COMFYUI_URL",
        "cloud_gpu": "VS_CLOUD_GPU_ENDPOINT",
    }.get(kind, "")
