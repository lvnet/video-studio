"""云 GPU / 多机分体后端（接口做实，默认桩、可切真实协议）。

方案 C 模式：控制在控制面、执行在 Agent，Agent 主动外连注册能力标签，
控制面不拉取 NAT 后文件；产物由 Agent 推对象存储，返回 storage_uri + sha256。

实现的协议（桩模式全内存模拟）：
- POST /agents/register       Agent 注册（能力标签）
- GET  /agents                节点列表与心跳状态
- POST /tasks                 按能力匹配下发
- GET  /tasks/{task_id}       状态回报
- POST /tasks/{task_id}/cancel 取消
- 产物：Agent 推对象存储，fetch 返回规范化引用

默认 ``dry_run=True``，不发真实请求。保留旧签名 ``CloudGpuBackend(control_endpoint)``。
"""

from __future__ import annotations

import hashlib
from typing import Any

from .base import Artifact, Backend, BackendKind, Cost, FailureCode, Progress, GenRequest
from .config import ComputeConfig
from .http_client import HttpClient, HttpError, StubHttpClient, TransportError, UrllibHttpClient

# 能力匹配：任务 mode -> 节点须声明支持的 mode 标签。
# 约定节点上报 capabilities_json 的 mode 用 r2v/i2v/t2v（见方案能力匹配）。
# r2v 节点还应带 ref2v=true 或 ref2va 权重标签，避免悄悄回退 FL2VA 丢身份锁定。
MODE_REQUIREMENT = {
    "r2v": "r2v",
    "i2v": "i2v",
    "t2v": "t2v",
}


class CloudGpuBackend(Backend):
    def __init__(
        self,
        control_endpoint: str = "",
        config: ComputeConfig | None = None,
        client: HttpClient | None = None,
    ):
        self.config = config or ComputeConfig(
            kind="cloud_gpu", base_url=control_endpoint, dry_run=True)
        self.control_endpoint = control_endpoint or self.config.base_url
        self.dry_run = self.config.dry_run
        self._client = client or self._default_client()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._agents: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def _default_client(self) -> HttpClient:
        if self.dry_run:
            return StubHttpClient(self._stub_responder)
        return UrllibHttpClient(self.control_endpoint)

    def set_client(self, client: HttpClient) -> None:
        self._client = client

    @property
    def client(self) -> HttpClient:
        return self._client

    @property
    def kind(self) -> BackendKind:
        return BackendKind.CLOUD_GPU

    # ---- Agent 注册与能力匹配（云 GPU 特有） -----------------------------
    def register_agent(self, agent_id: str, capabilities: dict[str, Any]) -> dict[str, Any]:
        """Agent 主动注册能力标签，如 {model:h3, mode:[r2v,i2v], vram:24, upscale:seedvr2}。"""
        rec = {"agent_id": agent_id, "capabilities": dict(capabilities),
               "last_heartbeat": "now", "status": "online"}
        if not self.dry_run:
            resp = self._client.post("/agents/register", json_body=rec,
                                     timeout=self.config.timeout)
            return resp.data if isinstance(resp.data, dict) else rec
        self._agents[agent_id] = rec
        return rec

    def heartbeat(self, agent_id: str) -> bool:
        if not self.dry_run:
            try:
                resp = self._client.post(f"/agents/{agent_id}/heartbeat",
                                         json_body={}, timeout=self.config.timeout)
                return resp.ok
            except (HttpError, TransportError):
                return False
        if agent_id in self._agents:
            self._agents[agent_id]["last_heartbeat"] = "now"
            return True
        return False

    def agents(self) -> list[dict[str, Any]]:
        if not self.dry_run:
            resp = self._client.get("/agents", timeout=self.config.timeout)
            return resp.data.get("agents", []) if isinstance(resp.data, dict) else []
        return list(self._agents.values())

    def _match_agent(self, mode: str) -> str | None:
        """按能力标签筛选候选节点；无 r2v 能力的节点不会被派 r2v 任务。"""
        need = MODE_REQUIREMENT.get(mode, "")
        for rec in self._agents.values():
            if rec.get("status") != "online":
                continue
            caps = rec.get("capabilities", {})
            modes = caps.get("mode", [])
            if isinstance(modes, str):
                modes = [modes]
            if not need or need in modes:
                return str(rec["agent_id"])
        return None

    # ---- Backend 接口 -----------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        if not self.dry_run:
            try:
                resp = self._client.get("/agents", timeout=self.config.timeout)
                return {"mode": "live", "agents": resp.data}
            except (HttpError, TransportError):
                return {"mode": "live", "agents": [], "reachable": False}
        return {"mode": "stub", "agents": self.agents(),
                "notes": "默认桩，未连接真实 Agent 集群"}

    def health(self) -> bool:
        if self.dry_run:
            return True
        try:
            resp = self._client.get("/agents", timeout=self.config.timeout)
            return resp.ok
        except (HttpError, TransportError):
            return False

    def estimate(self, task: GenRequest) -> Cost:
        # 云 GPU 按机时折算，设上限：默认 ¥3/h，按预计 GPU 分钟计费
        frames = int(task.payload.get("frames", 72))
        minutes = frames / 24 * 2.0
        rate = float(task.payload.get("gpu_rate_per_hour", 3.0))
        amount = round(minutes / 60 * rate, 4)
        return Cost(estimated=amount, currency="CNY",
                    breakdown={"mode": "cloud_gpu", "gpu_minutes": round(minutes, 2),
                               "rate_per_hour": rate, "capped": True})

    def submit(self, task: GenRequest) -> str:
        mode = str(task.payload.get("mode", "t2v"))
        agent = self._match_agent(mode) if self.dry_run else None
        body = {"task_id": task.task_id, "mode": mode, "payload": task.payload,
                "agent_id": agent, "idempotency_key": task.task_id}
        if not self.dry_run:
            resp = self._client.post("/tasks", json_body=body, timeout=self.config.timeout)
            data = resp.data if isinstance(resp.data, dict) else {}
            task_id = str(data.get("task_id") or task.task_id)
            self._tasks[task_id] = {"state": "leased", "agent_id": data.get("agent_id", ""),
                                     "lease_expiry": "30s"}
            return task_id

        self._counter += 1
        task_id = hashlib.sha1(
            ("gpu" + str(task.payload)).encode("utf-8")
        ).hexdigest()[:12] + f"{self._counter:02d}"
        # 无在线节点也能建任务（queued 待派），有节点则 leased
        state = "leased" if agent else "queued"
        self._tasks[task_id] = {"state": state, "agent_id": agent or "",
                                 "lease_expiry": "30s" if agent else ""}
        return task_id

    def poll(self, task_id: str) -> Progress:
        if not self.dry_run:
            resp = self._client.get(f"/tasks/{task_id}", timeout=self.config.timeout)
            return self._progress_from_data(task_id, resp.data)
        st = self._tasks.get(task_id)
        if not st:
            return Progress(task_id, "unknown")
        if st["state"] == "queued":
            st["state"] = "leased"
        elif st["state"] == "leased":
            st["state"] = "running"
        elif st["state"] == "running":
            st["state"] = "succeeded"
        return Progress(task_id, st["state"],
                        1.0 if st["state"] == "succeeded" else 0.5,
                        f"云 GPU 桩任务 {st['state']}")

    def _progress_from_data(self, task_id: str, data: Any) -> Progress:
        d = data if isinstance(data, dict) else {}
        state = str(d.get("status", "unknown")).lower()
        mapping = {"pending": "queued", "leased": "leased", "running": "running",
                   "succeeded": "succeeded", "success": "succeeded", "failed": "failed"}
        state = mapping.get(state, state)
        if state == "succeeded" and d.get("storage_uri"):
            self._tasks.setdefault(task_id, {})["artifact"] = d
        return Progress(task_id, state, float(d.get("progress", 0.5)),
                        str(d.get("message", "")))

    def fetch(self, task_id: str) -> Artifact:
        """产物由 Agent 推对象存储，本后端只返回规范化引用。"""
        st = self._tasks.get(task_id, {})
        if not self.dry_run:
            d = st.get("artifact", {})
            uri = str(d.get("storage_uri") or "")
            sha = str(d.get("sha256") or "")
        else:
            if not st:
                raise RuntimeError(f"任务 {task_id} 不存在，无法取产物")
            for _ in range(6):
                if self.poll(task_id).state == "succeeded":
                    break
            uri = f"object://cloud-gpu/{task_id}.mp4"
            sha = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        if not uri:
            raise RuntimeError(f"任务 {task_id} 尚无 Agent 推送的产物引用")
        if not sha:
            sha = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        return Artifact(storage_uri=uri, sha256=sha, media_type="video/mp4",
                        metadata={"task_id": task_id, "backend": "cloud_gpu",
                                  "agent_id": st.get("agent_id", ""),
                                  "mode": "stub" if self.dry_run else "live"})

    def cancel(self, task_id: str) -> bool:
        if not self.dry_run:
            try:
                self._client.post(f"/tasks/{task_id}/cancel",
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
        if any(k in text for k in ("node dead", "offline", "heartbeat", "lease")):
            return FailureCode.NODE_DEAD
        if any(k in text for k in ("timeout", "429", "rate limit")):
            return FailureCode.TRANSIENT
        return FailureCode.UNKNOWN

    # ---- 桩 responder -----------------------------------------------------
    def _stub_responder(self, method: str, path: str, params: dict, body: dict | None) -> Any:
        if path == "/agents/register":
            return body or {}
        if path == "/agents":
            return {"agents": self.agents()}
        if path == "/tasks" and method == "POST":
            return {"task_id": (body or {}).get("task_id", "stub-gpu-task"),
                    "agent_id": (body or {}).get("agent_id", "")}
        if path.startswith("/tasks/") and method == "GET":
            return {"status": "succeeded", "progress": 1.0}
        return {"ok": True}
