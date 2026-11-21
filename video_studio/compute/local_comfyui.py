"""本地 ComfyUI 后端（接口做实，默认内存桩、可切真实 HTTP）。

实现的 ComfyUI HTTP API：
- GET  /system_stats          健康检查
- GET  /object_info           能力发现（可用节点 / 模型）
- POST /prompt                提交任务（带 workflow graph）
- GET  /queue                 队列状态
- GET  /history/{prompt_id}   轮询结果
- GET  /view                  取回产物字节
- POST /upload/image          上传参考图（multipart）
- POST /interrupt             取消

默认 ``dry_run=True``：以上全部在内存里模拟，不发任何真实请求、零成本、
零版权风险。显式关闭 dry_run 并注入 ``UrllibHttpClient`` 才走真实协议。
产物由 Worker 推对象存储；本后端落本地目录并计算 sha256，返回规范化契约。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .base import Artifact, Backend, BackendKind, Cost, FailureCode, Progress, GenRequest
from .comfyui_workflow import build_workflow, outputs_from_history
from .config import ComputeConfig
from .http_client import (
    FakeHttpClient,
    HttpClient,
    HttpError,
    Response,
    StubHttpClient,
    TransportError,
    UrllibHttpClient,
)


class LocalComfyUIBackend(Backend):
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        config: ComputeConfig | None = None,
        client: HttpClient | None = None,
    ):
        self.config = config or ComputeConfig(kind="local_comfyui", base_url=base_url)
        self.base_url = self.config.base_url or base_url
        self.dry_run = self.config.dry_run
        self._client = client or self._default_client()
        self._tasks: dict[str, dict[str, Any]] = {}  # prompt_id -> 内部状态
        self._outputs: dict[str, list[dict[str, Any]]] = {}
        self._counter = 0

    # ---- 客户端 -----------------------------------------------------------
    def _default_client(self) -> HttpClient:
        if self.dry_run:
            return StubHttpClient(self._stub_responder)
        return UrllibHttpClient(self.base_url)

    def set_client(self, client: HttpClient) -> None:
        """注入传输层（测试用假客户端 / 生产用真实客户端）。"""
        self._client = client

    @property
    def client(self) -> HttpClient:
        return self._client

    # ---- Backend 接口 -----------------------------------------------------
    @property
    def kind(self) -> BackendKind:
        return BackendKind.LOCAL_COMFYUI

    def capabilities(self) -> dict[str, Any]:
        """能力发现：真实模式 GET /object_info，桩模式返回声明式能力。"""
        if not self.dry_run:
            try:
                resp = self._client.get("/object_info", timeout=self.config.timeout)
                nodes = resp.data if isinstance(resp.data, dict) else {}
                return {
                    "base_url": self.base_url,
                    "mode": "live",
                    "nodes": sorted(nodes.keys()),
                    "node_count": len(nodes),
                }
            except (HttpError, TransportError):
                return {"base_url": self.base_url, "mode": "live", "nodes": [], "reachable": False}
        return {
            "base_url": self.base_url,
            "mode": "stub",
            "nodes": ["CLIPTextEncode", "VSVideoGenerate", "SaveVideo"],
            "node_count": 3,
            "notes": "默认内存桩，未连接真实 ComfyUI",
        }

    def health(self) -> bool:
        if self.dry_run:
            return True  # 内存桩总是就绪
        try:
            resp = self._client.get("/system_stats", timeout=self.config.timeout)
            return resp.ok and isinstance(resp.data, dict)
        except (HttpError, TransportError):
            return False

    def estimate(self, task: GenRequest) -> Cost:
        # 本地算力无现金成本，按帧数折算预计 GPU 分钟供调度参考
        frames = int(task.payload.get("frames", 72))
        est_min = round(frames / 24 * 2.0, 2)
        return Cost(estimated=0.0, currency="CNY",
                    breakdown={"mode": "local", "est_gpu_minutes": est_min, "cash": 0.0})

    def submit(self, task: GenRequest) -> str:
        graph = build_workflow(task.payload)
        body = {"prompt": graph, "client_id": task.task_id}
        if not self.dry_run:
            resp = self._client.post("/prompt", json_body=body, timeout=self.config.timeout)
            data = resp.data if isinstance(resp.data, dict) else {}
            prompt_id = str(data.get("prompt_id") or data.get("id") or "")
            if not prompt_id:
                raise RuntimeError(f"/prompt 未返回 prompt_id：{data}")
            self._tasks[prompt_id] = {"prompt_id": prompt_id, "state": "queued", "graph": graph}
            return prompt_id

        # 桩：生成稳定 id，记录提交的图，状态 queued
        self._counter += 1
        prompt_id = hashlib.sha1(
            json.dumps(graph, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12] + f"{self._counter:02d}"
        self._tasks[prompt_id] = {"prompt_id": prompt_id, "state": "queued", "graph": graph}
        return prompt_id

    def poll(self, task_id: str) -> Progress:
        if not self.dry_run:
            try:
                resp = self._client.get(f"/history/{task_id}", timeout=self.config.timeout)
                return self._progress_from_history(task_id, resp.data)
            except HttpError as e:
                if e.status == 404:
                    return Progress(task_id, "queued", 0.0, "排队中")
                raise
        st = self._tasks.get(task_id)
        if not st:
            return Progress(task_id, "unknown", 0.0, "未知任务")
        # 桩：访问一次推进一格，模拟 queued → running → succeeded
        if st["state"] == "queued":
            st["state"] = "running"
            st["progress"] = 0.5
        elif st["state"] == "running":
            st["state"] = "succeeded"
            st["progress"] = 1.0
            prefix = str(st["graph"].get("20", {}).get("inputs", {}).get("filename_prefix", "vs"))
            self._outputs[task_id] = [{
                "filename": f"{prefix}_00001.mp4", "subfolder": "", "type": "output",
            }]
        p = st.get("progress", 0.0)
        return Progress(task_id, st["state"], p, f"ComfyUI 桩任务 {st['state']}")

    def _progress_from_history(self, task_id: str, data: Any) -> Progress:
        entry = None
        if isinstance(data, dict):
            entry = data.get(task_id) or (next(iter(data.values())) if data else None)
        if not entry:
            return Progress(task_id, "queued", 0.0, "排队中")
        status = (entry.get("status") or {})
        status_str = str(status.get("status_str", "")).lower()
        completed = bool(status.get("completed"))
        if "error" in status_str:
            return Progress(task_id, "failed", 0.0, "ComfyUI 执行出错")
        outs = outputs_from_history(entry)
        if completed or outs:
            self._outputs[task_id] = outs
            return Progress(task_id, "succeeded", 1.0, "已完成")
        return Progress(task_id, "running", 0.5, "生成中")

    def fetch(self, task_id: str) -> Artifact:
        """取回产物：GET /view 拉字节 → 落盘 → sha256 → 规范化 storage_uri。"""
        st = self._tasks.get(task_id)
        if st is None:
            raise RuntimeError(f"任务 {task_id} 不存在，无法取产物")
        if st.get("state") != "succeeded":
            # 桩模式：推进真实存储里的状态对象直到完成（最多 6 步，防死循环）
            if self.dry_run:
                for _ in range(6):
                    if self.poll(task_id).state == "succeeded":
                        break
        outputs = self._outputs.get(task_id) or []
        if not outputs:
            raise RuntimeError(f"任务 {task_id} 暂无可取产物")

        item = outputs[0]
        params = {
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        }
        resp = self._client.get("/view", params=params, timeout=self.config.timeout)
        raw = resp.data if isinstance(resp.data, bytes) else json.dumps(resp.data).encode("utf-8")
        sha256 = hashlib.sha256(raw).hexdigest()

        out_dir = self.config.output_dir or str(Path(os.environ.get("VS_DATA_DIR", ".")) / "outputs")
        target_dir = Path(out_dir) / item.get("subfolder", "")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / item["filename"]
        target.write_bytes(raw)
        uri = "local://" + str(target.resolve()).replace("\\", "/")
        return Artifact(
            storage_uri=uri,
            sha256=sha256,
            media_type="video/mp4",
            size_bytes=len(raw),
            metadata={"task_id": task_id, "backend": "local_comfyui", "mode": "stub" if self.dry_run else "live"},
        )

    def cancel(self, task_id: str) -> bool:
        if not self.dry_run:
            try:
                self._client.post("/interrupt", json_body={}, timeout=self.config.timeout)
            except (HttpError, TransportError):
                return False
        st = self._tasks.pop(task_id, None)
        self._outputs.pop(task_id, None)
        return st is not None

    def normalize_error(self, provider_error: Any) -> FailureCode:
        text = str(provider_error).lower()
        if isinstance(provider_error, HttpError):
            if provider_error.status in {429, 502, 503, 504}:
                return FailureCode.TRANSIENT
            if 400 <= provider_error.status < 500:
                return FailureCode.INVALID_PARAMS
        if isinstance(provider_error, TransportError):
            return FailureCode.TRANSIENT
        if "node" in text and ("dead" in text or "offline" in text):
            return FailureCode.NODE_DEAD
        if "oom" in text or "out of memory" in text:
            return FailureCode.TRANSIENT
        if "invalid" in text or "prompt" in text and "error" in text:
            return FailureCode.INVALID_PARAMS
        return FailureCode.UNKNOWN

    # ---- ComfyUI 扩展接口 -------------------------------------------------
    def queue(self) -> dict[str, Any]:
        """GET /queue：运行中 + 排队中任务。"""
        if not self.dry_run:
            resp = self._client.get("/queue", timeout=self.config.timeout)
            return resp.data if isinstance(resp.data, dict) else {}
        running = [k for k, v in self._tasks.items() if v["state"] == "running"]
        pending = [k for k, v in self._tasks.items() if v["state"] == "queued"]
        return {"queue_running": running, "queue_pending": pending}

    def upload_image(self, filename: str, data: bytes, subfolder: str = "") -> dict[str, Any]:
        """POST /upload/image：参考图上传。桩模式返回名字，不发请求。

        真实模式用 multipart/form-data（标准库构造），不引入第三方依赖。
        """
        if self.dry_run:
            return {"name": filename, "subfolder": subfolder, "type": "input"}
        boundary = "----vsboundary"
        parts = []
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
                     f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        parts.append(data)
        parts.append(b"\r\n")
        if subfolder:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n{subfolder}\r\n'.encode())
        parts.append(f'--{boundary}--\r\n'.encode())
        body = b"".join(parts)
        resp = self._client.post(
            "/upload/image", json_body=None,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=self.config.timeout,
        )
        return resp.data if isinstance(resp.data, dict) else {}

    # ---- 桩 responder：让 FakeHttpClient/默认桩也走协议路径 ----------------
    def _stub_responder(self, method: str, path: str, params: dict, body: dict | None) -> Any:
        if path == "/system_stats":
            return {"system": {"comfyui_version": "stub"}, "devices": []}
        if path == "/object_info":
            return {n: {} for n in ["CLIPTextEncode", "VSVideoGenerate", "SaveVideo"]}
        if path == "/prompt" and method == "POST":
            return {"prompt_id": "stub-prompt", "number": 0}
        if path.startswith("/history/"):
            pid = path.split("/")[-1]
            outs = self._outputs.get(pid, [])
            return {pid: {"status": {"status_str": "success", "completed": True},
                          "outputs": {"20": {"gifs": outs}}}}
        if path == "/view":
            # 产物字节：桩返回确定性内容，保证 sha256 可复现
            return Response(200, f"stub-video:{params.get('filename')}".encode("utf-8"),
                            {"Content-Type": "video/mp4"})
        if path == "/queue":
            return self.queue()
        if path == "/interrupt":
            return {}
        return {"ok": True}
