"""云端模型 API 后端（接口做实，默认桩、可切真实 HTTP）。

对齐方案 MVP 首选（MiniMax H3 等），但：
- 鉴权只从 ``credentials_ref`` 指向的环境变量读取，密钥不明文入库/下发；
- 计价可配置（按秒/分辨率），提交前给估算，不硬编码刊例价；
- 产物在云端，fetch 为上传 no-op：返回云端 storage_uri + sha256；
- 默认 ``dry_run=True``：不发真实请求、零成本。要真连需显式关闭并注入客户端。

保留旧签名 ``CloudApiBackend(api_key, endpoint, model)`` 兼容既有调用。
"""

from __future__ import annotations

import hashlib
from typing import Any

from .base import Artifact, Backend, BackendKind, Cost, FailureCode, Progress, GenRequest
from .config import ComputeConfig
from .http_client import HttpClient, HttpError, StubHttpClient, TransportError, UrllibHttpClient

# 默认计价（CNY/秒），可被 payload.price_per_second 覆盖；仅用于预估
DEFAULT_PRICE_PER_SECOND = 0.18
RESOLUTION_FACTOR = {"768P": 1.0, "1080P": 1.6, "2K": 2.6}


class CloudApiBackend(Backend):
    def __init__(
        self,
        api_key: str = "",
        endpoint: str = "",
        model: str = "MiniMax-H3",
        config: ComputeConfig | None = None,
        client: HttpClient | None = None,
    ):
        self.config = config or ComputeConfig(
            kind="cloud_api", base_url=endpoint, model=model,
            credentials_ref="", dry_run=True,
        )
        # 兼容旧签名：显式传入的 api_key 视为直接密钥（调用方负责来源）
        self._inline_key = api_key if api_key and api_key != "stub-key" else ""
        self.endpoint = endpoint or self.config.base_url
        self.model = model if model != "MiniMax-H3" else (self.config.model or model)
        self.dry_run = self.config.dry_run
        self._client = client or self._default_client()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def _default_client(self) -> HttpClient:
        if self.dry_run:
            return StubHttpClient(self._stub_responder)
        return UrllibHttpClient(self.endpoint, self._auth_headers())

    def _auth_headers(self) -> dict[str, str]:
        key = self.credential()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def credential(self) -> str:
        return self._inline_key or self.config.credential()

    def set_client(self, client: HttpClient) -> None:
        self._client = client

    @property
    def client(self) -> HttpClient:
        return self._client

    @property
    def kind(self) -> BackendKind:
        return BackendKind.CLOUD_API

    def capabilities(self) -> dict[str, Any]:
        if not self.dry_run:
            try:
                resp = self._client.get("/v1/models", timeout=self.config.timeout)
                return {"model": self.model, "mode": "live",
                        "models": resp.data, "modality": ["text", "image", "video"]}
            except (HttpError, TransportError):
                return {"model": self.model, "mode": "live", "models": [], "reachable": False}
        return {"model": self.model, "mode": "stub",
                "modality": ["text", "image", "video"],
                "notes": "默认桩，未调用真实云端 API"}

    def health(self) -> bool:
        if self.dry_run:
            return True
        if not self.credential():
            return False  # 无密钥直接判不健康，不发请求
        try:
            resp = self._client.get("/v1/models", timeout=self.config.timeout)
            return resp.ok
        except (HttpError, TransportError):
            return False

    def estimate(self, task: GenRequest) -> Cost:
        duration = float(task.payload.get("duration", task.payload.get("frames", 72) / 24))
        resolution = str(task.payload.get("resolution", "768P")).upper()
        factor = RESOLUTION_FACTOR.get(resolution, 1.0)
        price = float(task.payload.get("price_per_second", DEFAULT_PRICE_PER_SECOND))
        amount = round(duration * factor * price, 4)
        return Cost(estimated=amount, currency="CNY",
                    breakdown={"model": self.model, "seconds": duration,
                               "resolution": resolution, "factor": factor,
                               "price_per_second": price})

    def submit(self, task: GenRequest) -> str:
        body = self._request_body(task)
        if not self.dry_run:
            resp = self._client.post("/v1/video/generate", json_body=body,
                                     timeout=self.config.timeout)
            data = resp.data if isinstance(resp.data, dict) else {}
            task_id = str(data.get("task_id") or data.get("id") or "")
            if not task_id:
                raise RuntimeError(f"云端 API 未返回 task_id：{data}")
            self._tasks[task_id] = {"state": "queued"}
            return task_id

        self._counter += 1
        task_id = hashlib.sha1(
            (task.step + str(task.payload)).encode("utf-8")
        ).hexdigest()[:14] + f"{self._counter:02d}"
        self._tasks[task_id] = {"state": "queued", "body": body}
        return task_id

    def _request_body(self, task: GenRequest) -> dict[str, Any]:
        p = task.payload
        # 提示词已过上层合规守卫；这里只做协议字段映射，不复制任何版权素材
        return {
            "model": self.model,
            "prompt": p.get("prompt", ""),
            "negative_prompt": p.get("negative", ""),
            "mode": p.get("mode", "t2v"),
            "duration": p.get("duration", p.get("frames", 72) / 24),
            "resolution": p.get("resolution", "768P"),
            "seed": p.get("seed", 0),
            "reference_image": p.get("image", ""),
            "idempotency_key": task.task_id,
        }

    def poll(self, task_id: str) -> Progress:
        if not self.dry_run:
            resp = self._client.get(f"/v1/video/tasks/{task_id}", timeout=self.config.timeout)
            return self._progress_from_data(task_id, resp.data)
        st = self._tasks.get(task_id)
        if not st:
            return Progress(task_id, "unknown")
        if st["state"] == "queued":
            st["state"] = "running"
        elif st["state"] == "running":
            st["state"] = "succeeded"
        return Progress(task_id, st["state"],
                        1.0 if st["state"] == "succeeded" else 0.5,
                        f"云端桩任务 {st['state']}")

    def _progress_from_data(self, task_id: str, data: Any) -> Progress:
        d = data if isinstance(data, dict) else {}
        state = str(d.get("status", "unknown")).lower()
        mapping = {"pending": "queued", "queued": "queued", "in_progress": "running",
                   "running": "running", "succeeded": "succeeded", "success": "succeeded",
                   "completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}
        state = mapping.get(state, state)
        prog = float(d.get("progress", 1.0 if state == "succeeded" else 0.5))
        if state == "succeeded":
            self._tasks.setdefault(task_id, {})["artifact"] = d
        return Progress(task_id, state, prog, str(d.get("message", "")))

    def fetch(self, task_id: str) -> Artifact:
        """上传 no-op：产物在云端，返回 storage_uri + sha256。"""
        st = self._tasks.get(task_id, {})
        if not self.dry_run:
            d = st.get("artifact", {})
            uri = str(d.get("storage_uri") or d.get("url") or "")
            sha = str(d.get("sha256") or "")
        else:
            # 桩：推进真实存储里的状态对象直到完成（最多 6 步，防死循环）
            if not st:
                raise RuntimeError(f"任务 {task_id} 不存在，无法取产物")
            for _ in range(6):
                if self.poll(task_id).state == "succeeded":
                    break
            uri = f"cloud://{self.model}/{task_id}.mp4"
            sha = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        if not uri:
            raise RuntimeError(f"任务 {task_id} 云端产物尚无 storage_uri")
        if not sha:
            sha = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        return Artifact(storage_uri=uri, sha256=sha, media_type="video/mp4",
                        metadata={"task_id": task_id, "backend": "cloud_api",
                                  "mode": "stub" if self.dry_run else "live"})

    def cancel(self, task_id: str) -> bool:
        if not self.dry_run:
            try:
                self._client.post(f"/v1/video/tasks/{task_id}/cancel",
                                  json_body={}, timeout=self.config.timeout)
            except (HttpError, TransportError):
                return False
        return self._tasks.pop(task_id, None) is not None

    def normalize_error(self, provider_error: Any) -> FailureCode:
        text = str(provider_error).lower()
        if isinstance(provider_error, HttpError):
            if provider_error.status in {429, 502, 503, 504}:
                return FailureCode.TRANSIENT
            if 400 <= provider_error.status < 500:
                return FailureCode.INVALID_PARAMS
        if isinstance(provider_error, TransportError):
            return FailureCode.TRANSIENT
        if any(k in text for k in ("timeout", "429", "rate limit", "temporar")):
            return FailureCode.TRANSIENT
        if any(k in text for k in ("invalid", "400", "parameter", "违规")):
            return FailureCode.INVALID_PARAMS
        if "budget" in text or "预算" in text:
            return FailureCode.OUT_OF_BUDGET
        return FailureCode.UNKNOWN

    # ---- 桩 responder -----------------------------------------------------
    def _stub_responder(self, method: str, path: str, params: dict, body: dict | None) -> Any:
        if path == "/v1/models":
            return {"data": [{"id": self.model}]}
        if path == "/v1/video/generate" and method == "POST":
            return {"task_id": "stub-cloud-task"}
        if path.startswith("/v1/video/tasks/"):
            return {"status": "succeeded", "progress": 1.0}
        return {"ok": True}
