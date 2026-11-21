"""算力接口做实测试：HTTP 客户端 / ComfyUI 全套 / 真实协议 / 配置安全 / 注册表。

关键约定：所有"真实协议"测试都注入假客户端验证请求构造与响应解析，
不发起任何真实网络请求——零外连、零成本、零版权风险。
"""

import os
import tempfile
import unittest

from video_studio.compute import (
    BackendKind,
    CloudApiBackend,
    CloudGpuBackend,
    ComputeConfig,
    FakeHttpClient,
    GenRequest,
    LocalComfyUIBackend,
    build_workflow,
    outputs_from_history,
)
from video_studio.compute.http_client import (
    HttpError,
    Response,
    StubHttpClient,
    TransportError,
    UrllibHttpClient,
)
from video_studio.compute.registry import BackendRegistry, default_registry
from video_studio.models import BackendConfig


def req(step="video", **payload):
    return GenRequest(task_id="t-" + step, step=step, payload=payload)


class WorkflowBuilderTests(unittest.TestCase):
    def test_t2v_graph_structure(self):
        g = build_workflow({"prompt": "原创", "mode": "t2v"})
        self.assertIn("10", g)
        self.assertEqual(g["10"]["class_type"], "VSVideoGenerate")
        self.assertEqual(g["10"]["inputs"]["mode"], "t2v")
        self.assertIn("20", g)  # SaveVideo

    def test_r2v_adds_reference_image(self):
        g = build_workflow({"prompt": "原创", "mode": "r2v", "reference_images": ["role.png"]})
        self.assertIn("4", g)  # LoadImage
        self.assertEqual(g["10"]["inputs"]["reference_image"], ["4", 0])

    def test_custom_workflow_overrides(self):
        custom = {"99": {"class_type": "MyNode", "inputs": {}}}
        self.assertEqual(build_workflow({"workflow": custom}), custom)

    def test_outputs_from_history(self):
        entry = {"outputs": {"9": {"gifs": [{"filename": "a.mp4", "subfolder": "", "type": "output"}]}}}
        out = outputs_from_history(entry)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["filename"], "a.mp4")


class HttpClientTests(unittest.TestCase):
    def test_stub_client_records_calls(self):
        c = StubHttpClient(lambda m, p, q, b: {"ok": m})
        c.get("/health")
        c.post("/x", json_body={"a": 1})
        self.assertEqual(len(c.calls), 2)
        self.assertEqual(c.calls[0]["method"], "GET")
        self.assertEqual(c.calls[1]["json_body"], {"a": 1})

    def test_fake_client_routes_and_raises(self):
        c = FakeHttpClient({("GET", "/ok"): {"v": 1}})
        self.assertEqual(c.get("/ok").data, {"v": 1})
        with self.assertRaises(HttpError):
            c.get("/missing")

    def test_fake_client_raises_injected_error(self):
        c = FakeHttpClient({("GET", "/e"): HttpError(503, "busy")})
        with self.assertRaises(HttpError) as ctx:
            c.get("/e")
        self.assertEqual(ctx.exception.status, 503)

    def test_urllib_client_builds_url_with_params(self):
        c = UrllibHttpClient("http://host:8188")
        url = c._url("/view", {"filename": "a b.mp4"})
        self.assertTrue(url.startswith("http://host:8188/view?"))
        self.assertIn("filename=a+b.mp4", url)


class ComfyUIBackendTests(unittest.TestCase):
    def test_stub_full_chain(self):
        b = LocalComfyUIBackend()
        self.assertTrue(b.health())
        self.assertEqual(b.capabilities()["mode"], "stub")
        tid = b.submit(req(prompt="原创", mode="r2v", reference_images=["a.png"], frames=48))
        self.assertEqual(b.poll(tid).state, "running")
        self.assertEqual(b.poll(tid).state, "succeeded")
        a = b.fetch(tid)
        self.assertTrue(a.storage_uri.startswith("local://"))
        self.assertEqual(len(a.sha256), 64)
        self.assertIn("comfyui", a.metadata["backend"])
        self.assertTrue(b.cancel(tid))

    def test_queue_and_upload_stub(self):
        b = LocalComfyUIBackend()
        tid = b.submit(req(frames=24))
        q = b.queue()
        self.assertIn(tid, q["queue_pending"])
        up = b.upload_image("role.png", b"bytes")
        self.assertEqual(up["name"], "role.png")

    def test_live_protocol_request_construction(self):
        """注入假客户端，验证 POST /prompt 携带 workflow graph 与 client_id。"""
        fake = FakeHttpClient({
            ("GET", "/system_stats"): {"system": {}},
            ("GET", "/object_info"): {"KSampler": {}, "SaveVideo": {}},
            ("POST", "/prompt"): {"prompt_id": "pid-9"},
            ("GET", "/history/pid-9"): {"pid-9": {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"9": {"gifs": [{"filename": "out.mp4"}]}},
            }},
            ("GET", "/view"): Response(200, b"VIDEO", {"Content-Type": "video/mp4"}),
            ("POST", "/interrupt"): {},
        })
        cfg = ComputeConfig(kind="local_comfyui", base_url="http://comfy:8188", dry_run=False)
        b = LocalComfyUIBackend(config=cfg, client=fake)
        self.assertTrue(b.health())
        self.assertEqual(b.capabilities()["node_count"], 2)
        tid = b.submit(req(prompt="原创", steps=15))
        self.assertEqual(tid, "pid-9")
        prompt_call = [c for c in fake.calls if c["path"] == "/prompt"][0]
        self.assertEqual(prompt_call["json_body"]["client_id"], "t-video")
        graph = prompt_call["json_body"]["prompt"]
        self.assertEqual(graph["10"]["inputs"]["steps"], 15)
        self.assertEqual(b.poll(tid).state, "succeeded")
        a = b.fetch(tid)
        self.assertEqual(a.size_bytes, 5)  # b"VIDEO"
        view_call = [c for c in fake.calls if c["path"] == "/view"][0]
        self.assertEqual(view_call["params"]["filename"], "out.mp4")

    def test_error_normalization(self):
        b = LocalComfyUIBackend()
        self.assertEqual(b.normalize_error(HttpError(503)), "transient")
        self.assertEqual(b.normalize_error(HttpError(400)), "invalid_params")
        self.assertEqual(b.normalize_error(TransportError("x")), "transient")
        self.assertEqual(b.normalize_error("node dead"), "node_dead")


class CloudApiBackendTests(unittest.TestCase):
    def test_estimate_configurable(self):
        b = CloudApiBackend()
        c = b.estimate(req(duration=10, resolution="1080P", price_per_second=0.2))
        self.assertAlmostEqual(c.estimated, 10 * 1.6 * 0.2, places=4)
        self.assertEqual(c.currency, "CNY")

    def test_credential_from_env_reference_only(self):
        os.environ["VS_TEST_API_KEY"] = "sk-secret"
        try:
            cfg = ComputeConfig(kind="cloud_api", credentials_ref="VS_TEST_API_KEY", dry_run=False)
            b = CloudApiBackend(config=cfg)
            self.assertEqual(b.credential(), "sk-secret")
            # 公共视图只有引用名，不含密钥明文
            pub = cfg.to_public_dict()
            self.assertEqual(pub["credentials_ref"], "VS_TEST_API_KEY")
            self.assertNotIn("sk-secret", str(pub))
        finally:
            os.environ.pop("VS_TEST_API_KEY", None)

    def test_live_protocol_and_failure_mapping(self):
        fake = FakeHttpClient({
            ("GET", "/v1/models"): {"data": [{"id": "M"}]},
            ("POST", "/v1/video/generate"): {"task_id": "api-1"},
            ("GET", "/v1/video/tasks/api-1"): {
                "status": "succeeded", "storage_uri": "https://cdn/x.mp4", "sha256": "abc",
            },
        })
        cfg = ComputeConfig(kind="cloud_api", base_url="https://api.x", model="M", dry_run=False)
        b = CloudApiBackend(config=cfg, client=fake)
        tid = b.submit(req(prompt="原创", duration=5))
        self.assertEqual(tid, "api-1")
        body = [c for c in fake.calls if c["path"] == "/v1/video/generate"][0]["json_body"]
        self.assertEqual(body["idempotency_key"], "t-video")
        self.assertEqual(b.poll(tid).state, "succeeded")
        a = b.fetch(tid)
        self.assertEqual(a.storage_uri, "https://cdn/x.mp4")
        self.assertEqual(a.sha256, "abc")  # 云端给出 sha 时原样透传

    def test_no_credential_not_healthy_in_live_mode(self):
        cfg = ComputeConfig(kind="cloud_api", credentials_ref="VS_ABSENT_KEY", dry_run=False)
        self.assertFalse(CloudApiBackend(config=cfg).health())


class CloudGpuBackendTests(unittest.TestCase):
    def test_capability_matching_r2v_not_fallback(self):
        g = CloudGpuBackend()
        g.register_agent("weak", {"mode": ["t2v", "i2v"], "vram": 24})
        g.register_agent("strong", {"mode": ["r2v"], "vram": 24, "upscale": "seedvr2"})
        r2v = g.submit(req(mode="r2v"))
        t2v = g.submit(req(mode="t2v"))
        self.assertEqual(g._tasks[r2v]["agent_id"], "strong")
        self.assertEqual(g._tasks[t2v]["agent_id"], "weak")

    def test_heartbeat_and_estimate_cap(self):
        g = CloudGpuBackend()
        g.register_agent("a", {"mode": ["t2v"]})
        self.assertTrue(g.heartbeat("a"))
        self.assertFalse(g.heartbeat("ghost"))
        c = g.estimate(req(frames=72, gpu_rate_per_hour=3.0))
        self.assertGreater(c.estimated, 0)
        self.assertIn("capped", c.breakdown)

    def test_live_agent_register_protocol(self):
        fake = FakeHttpClient({
            ("POST", "/agents/register"): {"agent_id": "a1", "status": "online"},
            ("GET", "/agents"): {"agents": [{"agent_id": "a1"}]},
            ("POST", "/tasks"): {"task_id": "gpu-1", "agent_id": "a1"},
            ("GET", "/tasks/gpu-1"): {"status": "succeeded", "storage_uri": "object://x"},
        })
        cfg = ComputeConfig(kind="cloud_gpu", base_url="http://ctrl", dry_run=False)
        g = CloudGpuBackend(config=cfg, client=fake)
        g.register_agent("a1", {"mode": ["t2v"]})
        self.assertEqual(len(g.agents()), 1)
        tid = g.submit(req(mode="t2v"))
        self.assertEqual(tid, "gpu-1")
        self.assertEqual(g.poll(tid).state, "succeeded")


class RegistryConfigTests(unittest.TestCase):
    def test_default_registry_has_three_backends(self):
        reg = default_registry()
        self.assertEqual(set(reg.available()), {
            BackendKind.CLOUD_API, BackendKind.LOCAL_COMFYUI, BackendKind.CLOUD_GPU})

    def test_switch_records_active(self):
        reg = default_registry()
        reg.set_active(BackendKind.CLOUD_GPU)
        self.assertEqual(reg.active, BackendKind.CLOUD_GPU)
        self.assertTrue(reg.status()["backends"][2]["active"])

    def test_register_config_uses_ref_not_secret(self):
        reg = BackendRegistry()
        cfg = BackendConfig(id="bc", type="cloud_api", endpoint="https://api.x",
                            model="M", credentials_ref="REF_ONLY")
        b = reg.register_config("cloud_api", cfg)
        self.assertEqual(b.kind, BackendKind.CLOUD_API)
        self.assertTrue(b.dry_run)  # 默认桩
        pub = reg.configs()["cloud_api"]
        self.assertEqual(pub["credentials_ref"], "REF_ONLY")
        self.assertNotIn("REF_ONLY", [v for v in pub.values() if isinstance(v, str) and "secret" in v.lower()])

    def test_status_public_safe(self):
        reg = default_registry()
        s = reg.status()
        self.assertEqual(len(s["backends"]), 3)
        for row in s["backends"]:
            self.assertIn("capabilities", row)
            self.assertIsInstance(row["healthy"], bool)


if __name__ == "__main__":
    unittest.main()
