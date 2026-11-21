"""工作台 HTTP API 集成测试。

用标准库起真实 HTTP 服务（临时数据目录、随机端口），走完整 HTTP 请求，
验证八步环节的 API 契约。零外部依赖；测试结束自动清理。
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


class ApiIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["VS_DATA_DIR"] = cls._tmp.name
        # 强制 api_server 用临时目录重建状态
        import web.api_server as api
        cls.api = api
        api.STATE = None
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_01_health(self):
        s, d = self.req("GET", "/api/health")
        self.assertEqual(s, 200)
        self.assertTrue(d["ok"])

    def test_02_pipeline(self):
        s, d = self.req("GET", "/api/pipeline")
        self.assertEqual(s, 200)
        self.assertEqual(len(d["steps"]), 8)

    def test_03_demo_init(self):
        s, d = self.req("POST", "/api/demo/init", {})
        self.assertEqual(s, 200)
        self.assertTrue(d["initialized"])
        self.assertGreater(d["counts"]["shots"], 0)
        self.assertGreater(d["counts"]["prompts"], 0)

    def test_04_projects(self):
        s, d = self.req("GET", "/api/projects")
        self.assertEqual(s, 200)
        self.assertGreaterEqual(len(d["projects"]), 1)
        self.__class__.pid = d["projects"][0]["id"]

    def test_05_shots(self):
        s, d = self.req("GET", "/api/shots")
        self.assertEqual(s, 200)
        self.assertGreater(len(d["shots"]), 0)
        self.__class__.shot_id = d["shots"][0]["id"]

    def test_06_asset_approve(self):
        s, d = self.req("GET", "/api/assets")
        aid = d["assets"][0]["id"]
        s, d = self.req("POST", f"/api/assets/{aid}/approve", {})
        self.assertEqual(s, 200)
        self.assertTrue(d["asset"]["approved"])

    def test_07_prompt_expand(self):
        s, d = self.req("POST", "/api/prompts/expand",
                        {"shot_id": self.shot_id, "mode": "r2v"})
        self.assertEqual(s, 200)
        self.assertTrue(d["compliance"]["passed"])
        self.assertIn("six_segment", d["expanded"])

    def test_08_compliance_block(self):
        # 版权红线：复刻具名 IP 角色应被拦
        s, d = self.req("POST", "/api/compliance/check",
                        {"text": "画一个皮卡丘在打架", "subject": "test"})
        self.assertEqual(s, 200)
        self.assertFalse(d["passed"])

    def test_09_cost_check(self):
        s, d = self.req("POST", "/api/cost/check",
                        {"project_id": self.pid, "estimate_cents": 100})
        self.assertEqual(s, 200)
        self.assertIn("allowed", d)

    def test_10_task_submit_dispatch(self):
        s, d = self.req("POST", "/api/tasks",
                        {"shot_id": self.shot_id, "project_id": self.pid,
                         "estimate_cents": 50, "mode": "r2v"})
        self.assertEqual(s, 200)
        tid = d["task"]["id"]
        self.assertEqual(d["task"]["status"], "queued")
        s, d = self.req("POST", f"/api/tasks/{tid}/dispatch", {})
        self.assertEqual(s, 200)
        self.assertTrue(d["leased"])

    def test_11_task_failure_retry(self):
        s, d = self.req("GET", "/api/tasks")
        tid = d["tasks"][0]["id"]
        s, d = self.req("POST", f"/api/tasks/{tid}/fail",
                        {"failure_code": "PROVIDER_TRANSIENT"})
        self.assertEqual(s, 200)
        self.assertIn("diagnosis", d)
        self.assertIn(d["new_state"], ("queued", "dead_letter"))

    def test_12_compose_plan(self):
        s, d = self.req("POST", "/api/compose/plan",
                        {"shots": [{"index": 0, "dialogue": "你好", "duration": 5}]})
        self.assertEqual(s, 200)
        self.assertIn("srt", d)
        self.assertIn("00:00:00", d["srt"])
        self.assertIn("plan", d)

    def test_13_audit_integrity(self):
        s, d = self.req("GET", "/api/audit")
        self.assertEqual(s, 200)
        self.assertGreater(len(d["events"]), 0)
        s, d = self.req("GET", "/api/audit/verify")
        self.assertEqual(s, 200)
        self.assertTrue(d["intact"])

    def test_14_unknown_route(self):
        s, d = self.req("GET", "/api/nope")
        self.assertEqual(s, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
