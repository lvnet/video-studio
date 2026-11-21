"""剧集「集数可修改」回归测试。

覆盖：制作设置增减集数（原来只能补不能减）、单集编辑（集序号冲突自动交换）、
删除单集并级联清理镜头/提示词/剧本版本。真实 HTTP 服务 + 临时数据目录，零外部依赖。
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


class EpisodeEditApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["VS_DATA_DIR"] = cls._tmp.name
        import web.api_server as api
        cls.api = api
        # DATA_DIR 是模块级常量（import 时锁定），必须显式覆盖并在结束时还原，
        # 否则本类会污染其他测试类共享的数据目录（demo/init 幂等会拿到脏项目）。
        cls._orig_data_dir = api.DATA_DIR
        cls._orig_state = api.STATE
        api.DATA_DIR = Path(cls._tmp.name)
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
        cls.api.STATE = cls._orig_state
        cls.api.DATA_DIR = cls._orig_data_dir
        cls._tmp.cleanup()

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def setUp(self):
        s, d = self._req("POST", "/api/projects", {
            "name": "集数测试", "aspect_ratio": "9:16", "style_preset": "realistic_3d",
        })
        self.assertEqual(s, 200)
        self.pid = d["project"]["id"]

    def _eps(self):
        s, d = self._req("GET", "/api/episodes/tree?project_id=" + self.pid)
        self.assertEqual(s, 200)
        return sorted(d["episodes"], key=lambda e: e["index"])

    def _settings(self, n, duration=60):
        return self._req("POST", f"/api/projects/{self.pid}/settings",
                         {"episode_count": n, "duration_target": duration})

    def test_01_episode_count_can_increase_and_decrease(self):
        s, d = self._settings(3)
        self.assertEqual(s, 200)
        self.assertEqual(d["created_episodes"], 3)
        self.assertEqual([e["index"] for e in self._eps()], [1, 2, 3])

        # 核心修复：集数调小，移除末尾多余的集
        s, d = self._settings(2)
        self.assertEqual(s, 200)
        self.assertEqual(d["removed_episodes"], 1)
        self.assertEqual([e["index"] for e in self._eps()], [1, 2])

        # 再次设成 2：幂等，不误删
        s, d = self._settings(2)
        self.assertEqual(d["removed_episodes"], 0)
        self.assertEqual(d["created_episodes"], 0)

        # 调大：补齐骨架
        s, d = self._settings(4)
        self.assertEqual(d["created_episodes"], 2)
        self.assertEqual([e["index"] for e in self._eps()], [1, 2, 3, 4])

    def test_02_edit_episode_index_title_duration(self):
        self._settings(3)
        e1, e2, _ = self._eps()
        s, d = self._req("POST", f"/api/episodes/{e1['id']}/update", {
            "index": 2, "title": "改过的标题", "duration_target": 90,
        })
        self.assertEqual(s, 200)
        self.assertEqual(d["episode"]["title"], "改过的标题")
        self.assertEqual(d["episode"]["duration_target"], 90)
        self.assertEqual(d["episode"]["index"], 2)
        # 集序号唯一：原第 2 集自动变为第 1 集
        eps = self._eps()
        self.assertEqual([e["index"] for e in eps], [1, 2, 3])
        self.assertEqual(eps[0]["id"], e2["id"])

        # 非法集序号被拒绝
        s, _ = self._req("POST", f"/api/episodes/{e1['id']}/update", {"index": 0})
        self.assertEqual(s, 400)

    def test_03_delete_episode_cascades(self):
        self._settings(2)
        eid = self._eps()[-1]["id"]
        # 造下游数据：剧本版本 + 镜头 + 提示词
        self.assertEqual(self._req("POST", "/api/script/rewrite", {
            "episode_id": eid, "instruction": "更简洁",
        })[0], 200)
        s, d = self._req("POST", "/api/shots/split", {
            "episode_id": eid, "text": "场景：旧码头\n林远：我回来了。\n（他望向江面）",
        })
        self.assertEqual(s, 200)
        self.assertGreater(d["total"], 0)
        self.assertEqual(self._req("POST", "/api/prompts/batch", {
            "episode_id": eid, "mode": "r2v",
        })[0], 200)

        st = self.api.get_state().store
        shot_ids = {s["id"] for s in st.all("shots") if s.get("episode_id") == eid}
        self.assertTrue(shot_ids)
        self.assertTrue([v for v in st.all("script_versions") if v.get("episode_id") == eid])

        s, d = self._req("DELETE", f"/api/episodes/{eid}")
        self.assertEqual(s, 200)
        self.assertGreater(d["removed_related"], 0)
        self.assertEqual([e["index"] for e in self._eps()], [1])
        self.assertEqual([s for s in st.all("shots") if s.get("episode_id") == eid], [])
        self.assertEqual([p for p in st.all("prompts") if p.get("shot_id") in shot_ids], [])
        self.assertEqual([v for v in st.all("script_versions") if v.get("episode_id") == eid], [])


    def test_04_reimport_replaces_without_accumulating(self):
        text = ("第一集 初遇\n场景：江边\n林远：你好。\n\n"
                "第二集 再会\n场景：渡口\n守渡人：终于来了。")
        # 先造 5 集骨架，并给第 1 集挂镜头
        self._settings(5)
        first = self._eps()[0]["id"]
        self._req("POST", "/api/shots/split", {
            "episode_id": first, "text": "场景：旧码头\n林远：我回来了。",
        })

        # 第一次导入：覆盖 5 集，重新分成 2 集
        s, d = self._req("POST", "/api/script/import-book", {
            "project_id": self.pid, "text": text,
        })
        self.assertEqual(s, 200)
        self.assertEqual(d["chapters"], 2)
        self.assertEqual(d["replaced"], 5)
        self.assertEqual([e["index"] for e in self._eps()], [1, 2])
        st = self.api.get_state().store
        self.assertEqual([x for x in st.all("shots") if x.get("episode_id") == first], [])

        # 第二次导入：不再累加，仍是 2 集
        s, d = self._req("POST", "/api/script/import-book", {
            "project_id": self.pid, "text": text,
        })
        self.assertEqual(d["chapters"], 2)
        self.assertEqual([e["index"] for e in self._eps()], [1, 2])

        # append 模式：显式追加到末尾，从 3 开始
        s, d = self._req("POST", "/api/script/import-book", {
            "project_id": self.pid, "text": text, "mode": "append",
        })
        self.assertEqual(d["replaced"], 0)
        self.assertEqual([e["index"] for e in self._eps()], [1, 2, 3, 4])


    def test_05_import_only_splits_on_title_lines(self):
        # 正文里出现的「第一集就全部知道真相…」不应被误切成新集；
        # 只有行首独立的「第X集」才算标题。标题里的中文数字也能解析。
        text = (
            "第一集 归来\n场景：江边\n林远：第一集就全部知道真相，只有男主不知道。\n"
            "第二集 夜渡\n场景：渡口\n守渡人：第48集 第七小节这段讲转化。\n"
            "第三集 未竟\n场景：码头"
        )
        s, d = self._req("POST", "/api/script/import-book", {
            "project_id": self.pid, "text": text,
        })
        self.assertEqual(s, 200)
        self.assertEqual(d["chapters"], 3)
        eps = self._eps()
        self.assertEqual([e["index"] for e in eps], [1, 2, 3])
        self.assertIn("归来", eps[0]["title"])
        self.assertIn("夜渡", eps[1]["title"])
        # 正文里的误匹配没有产生额外的集
        self.assertFalse(any("全部知道真相" in e["title"] for e in eps))
        self.assertFalse(any("第七小节" in e["title"] for e in eps))

    def test_06_generated_content_drives_full_pipeline(self):
        """梗概生成的正文能贯通拆镜→提示词→任务，不再占位空转。"""
        # setUp 已建项目，但本测试独立造集，避免依赖其他测试的状态
        gen = self._req("POST", "/api/script/generate-outline", {
            "project_id": self.pid,
            "synopsis": "旅人十年后回到江边码头，寻找失散的故人林远。",
            "episodes": 3,
        })
        self.assertEqual(gen[0], 200)
        self.assertEqual(len(gen[1]["episodes"]), 3)
        eid = gen[1]["episodes"][0]["episode_id"]

        split = self._req("POST", "/api/shots/split", {"episode_id": eid})
        self.assertEqual(split[0], 200)
        self.assertGreaterEqual(split[1]["total"], 2, "生成的正文必须能拆出多个镜头")

        prompts = self._req("POST", "/api/prompts/batch", {"episode_id": eid, "mode": "r2v"})
        self.assertEqual(prompts[0], 200)
        self.assertGreaterEqual(prompts[1]["generated"], 1)

        task = self._req("POST", "/api/tasks", {
            "shot_id": split[1]["shots"][0]["id"], "project_id": self.pid, "mode": "r2v",
        })
        self.assertEqual(task[0], 200)
        self.assertTrue(task[1].get("task") or task[1].get("task_id"))


if __name__ == "__main__":
    unittest.main()
