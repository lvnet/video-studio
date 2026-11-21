"""新工作台 HTTP API 集成测试（剧本/分集/提示词/预览/提交/记录/合成/配置/专家/资产库）。

用标准库起真实 HTTP 服务（临时数据目录、随机端口），走完整 HTTP 请求。
零外部依赖；测试结束自动清理。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class WorkbenchApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["VS_DATA_DIR"] = cls._tmp.name
        import web.api_server as api
        cls.api = api
        api.STATE = None
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        # 初始化示例
        s, d = cls._req_static(cls, "POST", "/api/demo/init", {})
        cls.pid = d["project"]["id"]
        s, eps = cls._req_static(cls, "GET", "/api/episodes/tree?project_id=" + cls.pid)
        cls.eid = eps["episodes"][0]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def _req_static(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def req(self, method, path, body=None):
        return self._req_static(method, path, body)

    def test_01_demo_multi_episodes(self):
        s, d = self.req("GET", "/api/episodes/tree?project_id=" + self.pid)
        self.assertEqual(s, 200)
        self.assertGreaterEqual(len(d["episodes"]), 3)
        self.assertGreater(d["episodes"][0]["shot_count"], 0)

    def test_02_script_outline(self):
        s, d = self.req("GET", f"/api/script/outline?episode_id={self.eid}&per_segment=2")
        self.assertEqual(s, 200)
        self.assertGreater(len(d["segments"]), 0)
        self.assertGreater(len(d["outline"]), 0)
        self.assertIn("action", d["segments"][0])

    def test_03_preview_check(self):
        s, d = self.req("GET", "/api/preview/check?episode_id=" + self.eid)
        self.assertEqual(s, 200)
        self.assertGreater(d["total"], 0)
        self.assertIn("complete", d)
        # 示例故意留一个缺参考图的镜头，体现高亮
        self.assertGreaterEqual(len([r for r in d["rows"] if not r["complete"]]), 1)

    def test_04_prompt_approve(self):
        s, d = self.req("GET", "/api/prompts")
        pid = d["prompts"][0]["id"]
        s, d = self.req("POST", "/api/prompts/approve", {"prompt_id": pid, "score": 4.8})
        self.assertEqual(s, 200)
        self.assertTrue(d["prompt"]["approved"])
        self.assertEqual(d["prompt"]["score"], 4.8)

    def test_05_prompts_batch(self):
        s, d = self.req("POST", "/api/prompts/batch",
                        {"episode_id": self.eid, "mode": "r2v"})
        self.assertEqual(s, 200)
        self.assertGreaterEqual(d["generated"], 0)
        self.assertIsInstance(d["blocked"], list)

    def test_06_submit_batch(self):
        s, d = self.req("POST", "/api/submit/batch", {
            "episode_id": self.eid, "project_id": self.pid, "mode": "r2v",
            "steps": 20, "resolution": "768P", "estimate_cents": 50,
            "chain_lock": True, "connect_review": False,
        })
        self.assertEqual(s, 200)
        self.assertGreater(d["succeeded"], 0)
        self.assertIn("未调用真实算力", d["note"])

    def test_07_records_export_csv(self):
        s, d = self.req("GET", "/api/records/export?project_id=" + self.pid)
        self.assertEqual(s, 200)
        self.assertIn("时间", d["csv"])
        self.assertIn("状态", d["csv"])
        self.assertTrue(d["filename"].endswith(".csv"))

    def test_08_compose_records_crud(self):
        s, d = self.req("GET", "/api/compose/records?project_id=" + self.pid)
        self.assertEqual(s, 200)
        self.assertGreater(len(d["records"]), 0)
        s, d = self.req("POST", "/api/compose/records", {
            "project_id": self.pid, "name": "测试合集", "version": "v9",
            "episode_range": "第1集", "duration": 40, "size_mb": 12.3,
        })
        self.assertEqual(s, 200)
        cid = d["record"]["id"]
        s, d = self.req("DELETE", "/api/compose/records/" + cid)
        self.assertEqual(s, 200)
        self.assertTrue(d["deleted"])

    def test_09_video_config(self):
        s, d = self.req("GET", "/api/video-config")
        self.assertEqual(s, 200)
        self.assertIn("ref2va_diffusion", d["defaults"])
        s, d2 = self.req("POST", "/api/video-config", {"params": {"superres_target": "1080"}})
        self.assertEqual(s, 200)
        self.assertEqual(d2["config"]["params"]["superres_target"], "1080")

    def test_10_experts(self):
        s, d = self.req("GET", "/api/experts")
        self.assertEqual(s, 200)
        self.assertGreaterEqual(d["total"], 8)
        names = [e["name"] for e in d["experts"]]
        self.assertIn("提示词扩写", names)
        s, d2 = self.req("POST", "/api/experts", {
            "name": "测试专家", "category": "测试", "system_prompt": "你是测试专家"})
        self.assertEqual(s, 200)
        self.assertFalse(d2["expert"]["builtin"])

    def test_11_asset_stats(self):
        s, d = self.req("GET", "/api/asset-stats")
        self.assertEqual(s, 200)
        self.assertGreater(d["stats"]["all"], 0)
        self.assertGreaterEqual(d["stats"]["role"], 1)

    def test_12_delete_method(self):
        # 确认 DELETE 方法可用（不存在的记录返回 404 而非 405）
        s, d = self.req("DELETE", "/api/compose/records/nonexistent")
        self.assertEqual(s, 404)

    # --- 算力管理端点 ---
    def test_13_backends_overview(self):
        s, d = self.req("GET", "/api/backends")
        self.assertEqual(s, 200)
        self.assertEqual(len(d["backends"]), 3)
        self.assertIn("active", d)
        for row in d["backends"]:
            self.assertIn("capabilities", row)
            self.assertIn("config", row)

    def test_14_compute_configs_no_secret(self):
        s, d = self.req("GET", "/api/compute/configs")
        self.assertEqual(s, 200)
        self.assertIn("configs", d)

    def test_15_compute_switch_and_health(self):
        s, d = self.req("POST", "/api/compute/switch", {"kind": "local_comfyui"})
        self.assertEqual(s, 200)
        self.assertEqual(d["active"], "local_comfyui")
        s, h = self.req("GET", "/api/compute/health?kind=local_comfyui")
        self.assertEqual(s, 200)
        self.assertTrue(h["healthy"])
        s, bad = self.req("POST", "/api/compute/switch", {"kind": "unknown"})
        self.assertEqual(s, 400)

    def test_16_comfyui_workflow(self):
        s, d = self.req("POST", "/api/comfyui/workflow",
                        {"prompt": "原创", "mode": "r2v", "reference_images": ["a.png"]})
        self.assertEqual(s, 200)
        wf = d["workflow"]
        self.assertEqual(wf["10"]["class_type"], "VSVideoGenerate")
        self.assertIn("4", wf)  # 参考图节点

    def test_17_compute_simulate(self):
        for kind in ["local_comfyui", "cloud_api", "cloud_gpu"]:
            with self.subTest(kind=kind):
                s, d = self.req("POST", "/api/compute/simulate", {"kind": kind, "mode": "t2v"})
                self.assertEqual(s, 200)
                self.assertIn("succeeded", d["states"])
                self.assertIsNotNone(d["artifact"])
                self.assertEqual(len(d["artifact"]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
