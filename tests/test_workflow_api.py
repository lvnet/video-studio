"""八步制作流程 API 集成测试（每步功能点做实）。

覆盖：工作流定义、项目骨架、剧本生成/导入/改写/版本链、拆镜/缺字段/编辑、
参考图绑定、清空未批准提示词、取消任务、候选/挑片/失败诊断/重试、精修装配/导出。
真实 HTTP 服务 + 临时数据目录，零外部依赖。
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


class WorkflowApiTest(unittest.TestCase):
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
        _, d = cls._req_static(cls, "POST", "/api/demo/init", {})
        cls.pid = d["project"]["id"]
        _, eps = cls._req_static(cls, "GET", "/api/episodes/tree?project_id=" + cls.pid)
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

    # --- 第 1 步：项目 ---
    def test_00_workflow_definition(self):
        s, d = self.req("GET", "/api/workflow/steps")
        self.assertEqual(s, 200)
        self.assertEqual(d["count"], 8)
        keys = [x["key"] for x in d["steps"]]
        self.assertEqual(keys, ["project", "script", "storyboard", "reference",
                                "prompt", "submit", "review", "finalize"])
        for step in d["steps"]:
            self.assertGreaterEqual(len(step["points"]), 2, step["key"])

    def test_01_project_settings(self):
        s, d = self.req("POST", f"/api/projects/{self.pid}/settings",
                        {"episode_count": 5, "duration_target": 45, "budget_total": 99})
        self.assertEqual(s, 200)
        self.assertGreaterEqual(d["created_episodes"], 1)
        self.assertEqual(d["project"]["budget_total"], 99)
        _, d2 = self.req("POST", f"/api/projects/{self.pid}/settings",
                         {"episode_count": 5, "duration_target": 45})
        self.assertEqual(d2["created_episodes"], 0)  # 幂等，不重复建集

    # --- 第 2 步：剧本 ---
    def test_02_script_generate_outline(self):
        s, d = self.req("POST", "/api/script/generate-outline",
                        {"project_id": self.pid, "synopsis": "原创测试梗概", "episodes": 2})
        self.assertEqual(s, 200)
        self.assertEqual(len(d["episodes"]), 2)
        self.assertTrue(all(e["compliance"] for e in d["episodes"]))

    def test_03_script_import_book(self):
        s, d = self.req("POST", "/api/script/import-book", {
            "project_id": self.pid,
            "text": "第一集 初遇\n场景：江边\n林远：你好。\n\n第二集 再会\n场景：渡口\n守渡人：终于来了。",
        })
        self.assertEqual(s, 200)
        self.assertEqual(d["chapters"], 2)

    def test_04_script_rewrite_and_versions(self):
        s, d = self.req("POST", "/api/script/rewrite",
                        {"episode_id": self.eid, "instruction": "更简洁"})
        self.assertEqual(s, 200)
        self.assertGreaterEqual(d["version"]["version"], 1)
        s, d2 = self.req("GET", "/api/script/versions?episode_id=" + self.eid)
        self.assertEqual(s, 200)
        self.assertGreater(d2["total"], 0)
        # 人工确认版本
        vid = d["version"]["id"]
        s, d3 = self.req("POST", "/api/script/versions/" + vid + "/confirm", {})
        self.assertEqual(s, 200)
        self.assertTrue(d3["version"]["confirmed"])

    # --- 第 3 步：拆镜 ---
    def test_05_shots_split_and_missing(self):
        s, d = self.req("POST", "/api/shots/split", {
            "episode_id": self.eid,
            "text": "场景：旧码头\n林远：我回来了。\n（他望向江面）",
        })
        self.assertEqual(s, 200)
        self.assertEqual(d["total"], 1)
        sid = d["shots"][0]["id"]
        s, d2 = self.req("POST", "/api/shots/" + sid + "/update", {"action": "走近江边"})
        self.assertEqual(s, 200)
        self.assertEqual(d2["shot"]["action"], "走近江边")
        s, d3 = self.req("GET", "/api/shots/missing?episode_id=" + self.eid)
        self.assertEqual(s, 200)
        self.assertIn("missing_fields", d3)

    # --- 第 4 步：参考资产 ---
    def test_06_shot_refs(self):
        shots = self.req("GET", "/api/shots?episode_id=" + self.eid)[1]["shots"]
        assets = self.req("GET", "/api/assets?project_id=" + self.pid)[1]["assets"]
        approved = [a["id"] for a in assets if a.get("approved")][:1]
        self.assertTrue(approved)
        s, d = self.req("POST", "/api/shots/" + shots[0]["id"] + "/refs",
                        {"asset_ids": approved, "asset_version": 1})
        self.assertEqual(s, 200)
        self.assertEqual(d["bound"], 1)
        # 未批准资产应被拒
        unapproved = [a["id"] for a in assets if not a.get("approved")]
        if unapproved:
            s, _ = self.req("POST", "/api/shots/" + shots[0]["id"] + "/refs",
                            {"asset_ids": unapproved})
            self.assertEqual(s, 403)

    # --- 第 5 步：提示词 ---
    def test_07_clear_unapproved_prompts(self):
        s, d = self.req("POST", "/api/prompts/clear-unapproved?episode_id=" + self.eid, {})
        self.assertEqual(s, 200)
        self.assertIn("removed", d)

    # --- 第 6 步：提交 / 取消 ---
    def test_08_cancel_tasks(self):
        s, d = self.req("POST", "/api/tasks/cancel", {"episode_id": self.eid})
        self.assertEqual(s, 200)
        self.assertIn("cancelled", d)

    # --- 第 7 步：挑片 ---
    def test_09_candidates_and_adopt(self):
        shots = self.req("GET", "/api/shots?episode_id=" + self.eid)[1]["shots"]
        sid = shots[0]["id"]
        s, d = self.req("POST", "/api/candidates/generate", {"shot_id": sid, "count": 3})
        self.assertEqual(s, 200)
        self.assertEqual(d["total"], 3)
        oid = d["candidates"][0]["id"]
        s, d2 = self.req("POST", "/api/outputs/" + oid + "/adopt", {})
        self.assertEqual(s, 200)
        self.assertTrue(d2["output"]["is_adopted"])
        s, d3 = self.req("GET", "/api/candidates?episode_id=" + self.eid)
        self.assertEqual(s, 200)
        self.assertGreater(len(d3["groups"]), 0)

    def test_10_failure_diagnosis_and_retry(self):
        # 造一个失败任务：提交后立即置失败码（新建独立镜头，不依赖其他测试的镜头数量）
        sid = self.req("POST", "/api/shots/split", {
            "episode_id": self.eid, "text": "场景：码头\n林远：我回来了。\n（他望向江面）",
        })[1]["shots"][0]["id"]
        s, sub = self.req("POST", "/api/tasks", {"shot_id": sid,
                                                  "project_id": self.pid, "mode": "t2v"})
        tid = sub["task"]["id"]
        self.req("POST", f"/api/tasks/{tid}/fail", {"failure_code": "PROVIDER_TRANSIENT"})
        s, d = self.req("GET", "/api/tasks/failures?episode_id=" + self.eid)
        self.assertEqual(s, 200)
        self.assertTrue(any(f["task"]["id"] == tid for f in d["failures"]))
        s, d2 = self.req("POST", f"/api/tasks/{tid}/retry", {})
        self.assertEqual(s, 200)
        self.assertEqual(d2["task"]["status"], "succeeded")

    # --- 第 8 步：精修成片 ---
    def test_11_finalize_plan_and_export(self):
        s, d = self.req("GET", f"/api/finalize/plan?project_id={self.pid}&episode_id={self.eid}&backend=local_comfyui")
        self.assertEqual(s, 200)
        self.assertIn("srt", d)
        self.assertIn("ffmpeg_script", d)
        self.assertEqual(d["plan"]["upscale"], "local_seedvr2")
        s, d2 = self.req("POST", "/api/finalize/export",
                         {"project_id": self.pid, "name": "测试精修成片", "version": "v8"})
        self.assertEqual(s, 200)
        self.assertEqual(d2["record"]["name"], "测试精修成片")


if __name__ == "__main__":
    unittest.main(verbosity=2)
