"""算力适配层测试：注册表 + 三种后端桩接口一致性（标准库 unittest）。"""

import unittest

from video_studio.compute import (
    BackendKind,
    BackendRegistry,
    CloudApiBackend,
    CloudGpuBackend,
    GenRequest,
    LocalComfyUIBackend,
    Task,
    default_registry,
)
from video_studio.models import Task as PersistentTask


class RegistryTests(unittest.TestCase):
    def test_register_get_and_cache(self):
        reg = BackendRegistry()
        created = []
        def factory():
            created.append(1)
            return LocalComfyUIBackend()
        reg.register(BackendKind.LOCAL_COMFYUI, factory)
        a = reg.get(BackendKind.LOCAL_COMFYUI)
        b = reg.get("local_comfyui")  # 字符串也能取
        self.assertIs(a, b)           # 实例被缓存
        self.assertEqual(len(created), 1)

    def test_available_kinds(self):
        reg = default_registry()
        self.assertEqual(set(reg.available()), {
            BackendKind.CLOUD_API, BackendKind.LOCAL_COMFYUI, BackendKind.CLOUD_GPU
        })

    def test_unregistered_raises(self):
        reg = BackendRegistry()
        with self.assertRaises(KeyError):
            reg.get(BackendKind.CLOUD_API)

    def test_register_by_string(self):
        reg = BackendRegistry()
        reg.register("cloud_api", lambda: CloudApiBackend("k", "http://x"))
        self.assertEqual(reg.get("cloud_api").kind, BackendKind.CLOUD_API)

    def test_re_register_clears_cached_instance(self):
        reg = BackendRegistry()
        reg.register(BackendKind.LOCAL_COMFYUI, LocalComfyUIBackend)
        first = reg.get(BackendKind.LOCAL_COMFYUI)
        reg.register(BackendKind.LOCAL_COMFYUI, LocalComfyUIBackend)
        second = reg.get(BackendKind.LOCAL_COMFYUI)
        self.assertIsNot(first, second)

    def test_switch_returns_target_backend(self):
        reg = default_registry()
        target = reg.switch("task-1", BackendKind.CLOUD_GPU)
        self.assertEqual(target.kind, BackendKind.CLOUD_GPU)


class BackendInterfaceTests(unittest.TestCase):
    def test_all_backends_share_interface(self):
        backends = [
            CloudApiBackend("k", "http://x"),
            LocalComfyUIBackend(),
            CloudGpuBackend(),
        ]
        required = ["capabilities", "health", "estimate", "submit", "poll",
                    "fetch", "cancel", "normalize_error"]
        for b in backends:
            for m in required:
                self.assertTrue(hasattr(b, m), f"{b.kind} 缺方法 {m}")

    def test_submit_poll_cancel_consistent(self):
        # 桩任务提交后状态可推进（不再停留在 queued）；取消后回到 unknown。
        for b in [CloudApiBackend("k", "http://x"), LocalComfyUIBackend(), CloudGpuBackend()]:
            with self.subTest(kind=b.kind):
                tid = b.submit(GenRequest(task_id="t", step="video", payload={"x": 1}))
                self.assertTrue(tid)
                self.assertIn(b.poll(tid).state, {"queued", "leased", "running", "succeeded"})
                self.assertTrue(b.cancel(tid))
                self.assertEqual(b.poll(tid).state, "unknown")

    def test_fetch_returns_canonical_artifact(self):
        # 接口做实后：桩 fetch 推进到完成并返回带 sha256 的规范化产物契约。
        for b in [CloudApiBackend("k", "http://x"), LocalComfyUIBackend(), CloudGpuBackend()]:
            with self.subTest(kind=b.kind):
                tid = b.submit(GenRequest(task_id="t", step="video", payload={"x": 1}))
                artifact = b.fetch(tid)
                self.assertTrue(artifact.storage_uri)
                self.assertEqual(len(artifact.sha256), 64)

    def test_fetch_unknown_task_raises(self):
        # 不存在的任务明确报错，而非静默返回假产物。
        for b in [CloudApiBackend("k", "http://x"), LocalComfyUIBackend(), CloudGpuBackend()]:
            with self.subTest(kind=b.kind):
                with self.assertRaises(RuntimeError):
                    b.fetch("nonexistent")

    def test_kinds_distinct(self):
        self.assertEqual(
            {CloudApiBackend("k", "x").kind, LocalComfyUIBackend().kind, CloudGpuBackend().kind},
            {BackendKind.CLOUD_API, BackendKind.LOCAL_COMFYUI, BackendKind.CLOUD_GPU},
        )


class RequestVsTaskDistinctionTests(unittest.TestCase):
    """架构修复：算力层请求与持久化任务实体不再混淆。"""

    def test_gen_request_and_persistent_task_are_distinct(self):
        self.assertIsNot(GenRequest, PersistentTask)

    def test_legacy_task_alias_points_to_gen_request(self):
        # 向后兼容：老代码 from video_studio.compute import Task 仍可用
        self.assertIs(Task, GenRequest)

    def test_fields_differ_as_expected(self):
        req = GenRequest(task_id="r", step="video", payload={})
        task = PersistentTask(id="t", shot_id="s", kind="t2v")
        self.assertTrue(hasattr(req, "payload"))
        self.assertFalse(hasattr(req, "shot_id"))
        self.assertTrue(hasattr(task, "shot_id"))
        self.assertFalse(hasattr(task, "payload"))


if __name__ == "__main__":
    unittest.main()
