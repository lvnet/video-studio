"""数据模型与本地持久化测试（标准库 unittest）。"""

import tempfile
import unittest
from pathlib import Path

from video_studio.models import (
    Asset,
    Episode,
    LocalStore,
    Output,
    Project,
    Shot,
    StoreError,
    Task,
    new_id,
)


class EntitySerializationTests(unittest.TestCase):
    def test_roundtrip(self):
        p = Project(id="p1", name="测试", budget_total=100.0)
        d = p.to_dict()
        self.assertEqual(d["name"], "测试")
        p2 = Project.from_dict(d)
        self.assertEqual(p2, p)

    def test_new_id_unique(self):
        ids = {new_id("x") for _ in range(100)}
        self.assertEqual(len(ids), 100)


class CrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_get_update_delete(self):
        self.store.add("projects", Project(id="p1", name="A"))
        self.assertEqual(self.store.get("projects", "p1")["name"], "A")
        self.store.update("projects", "p1", name="B")
        self.assertEqual(self.store.get("projects", "p1")["name"], "B")
        self.store.delete("projects", "p1")
        with self.assertRaises(StoreError):
            self.store.get("projects", "p1")

    def test_duplicate_id_rejected(self):
        self.store.add("projects", Project(id="p1", name="A"))
        with self.assertRaises(StoreError):
            self.store.add("projects", Project(id="p1", name="B"))

    def test_find_by_filter(self):
        self.store.add("projects", Project(id="p1", name="A", status="active"))
        self.store.add("projects", Project(id="p2", name="B", status="archived"))
        self.assertEqual(len(self.store.find("projects", status="active")), 1)

    def test_persistence_survives_restart(self):
        self.store.add("projects", Project(id="p1", name="持久化"))
        reopened = LocalStore(self.tmp.name)
        self.assertEqual(reopened.get("projects", "p1")["name"], "持久化")


class AdoptionTests(unittest.TestCase):
    """采用版唯一真源：outputs.is_adopted。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = LocalStore(self.tmp.name)
        self.s.add("projects", Project(id="p1", name="x"))
        self.s.add("episodes", Episode(id="e1", project_id="p1", index=1))
        self.s.add("shots", Shot(id="s1", episode_id="e1", index=1))
        self.s.add("tasks", Task(id="t1", shot_id="s1", kind="t2v"))
        for i in range(3):
            self.s.add("outputs", Output(id=f"o{i}", task_id="t1", url=f"o{i}.mp4"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_one_adopted_per_task(self):
        self.s.adopt_output("o0")
        self.s.adopt_output("o1")
        # 直接检查状态：o1 采用，o0 取消
        self.assertFalse(self.s.get("outputs", "o0")["is_adopted"])
        self.assertTrue(self.s.get("outputs", "o1")["is_adopted"])
        self.assertEqual(len(self.s.find("outputs", task_id="t1", is_adopted=True)), 1)

    def test_adopted_output_returns_one(self):
        self.s.adopt_output("o2")
        adopted = self.s.adopted_output("t1")
        self.assertIsNotNone(adopted)
        self.assertEqual(adopted["id"], "o2")

    def test_adopt_syncs_shot_redundant_field(self):
        self.s.adopt_output("o1")
        self.assertEqual(self.s.get("shots", "s1")["adopted_task_id"], "t1")


class AssetLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = LocalStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lock_asset_version(self):
        ta = self.s.lock_asset_version("t1", "asset1", version=3, is_primary=True)
        self.assertEqual(ta.asset_version, 3)
        locked = self.s.asset_versions_for_task("t1")
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0]["asset_version"], 3)
        self.assertTrue(locked[0]["is_primary"])


class CostLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = LocalStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ledger_default_and_save(self):
        ledger = self.s.ledger("p1")
        self.assertEqual(ledger.spent_cents, 0)
        ledger.spent_cents = 500
        ledger.reserved_cents = 100
        self.s.save_ledger(ledger)
        reopened = LocalStore(self.tmp.name)
        self.assertEqual(reopened.ledger("p1").spent_cents, 500)


if __name__ == "__main__":
    unittest.main()
