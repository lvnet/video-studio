"""骨架冒烟测试：验证状态机与八步链定义一致（标准库 unittest，零依赖）。"""

import unittest

from video_studio.compute.base import BackendKind, FailureCode
from video_studio.control.state import TaskState, can_transition, next_after_failure
from video_studio.creation.pipeline import PIPELINE, Step, next_step


class SkeletonTests(unittest.TestCase):
    def test_pipeline_has_eight_steps(self):
        self.assertEqual(len(PIPELINE), 8)
        self.assertEqual(PIPELINE[0], Step.IDEA)
        self.assertEqual(PIPELINE[-1], Step.COMPOSE)

    def test_next_step(self):
        self.assertEqual(next_step(Step.IDEA), Step.SCRIPT)
        self.assertIsNone(next_step(Step.COMPOSE))

    def test_state_machine_happy_path(self):
        path = [
            (TaskState.CREATED, TaskState.QUEUED),
            (TaskState.QUEUED, TaskState.LEASED),
            (TaskState.LEASED, TaskState.RUNNING),
            (TaskState.RUNNING, TaskState.UPLOADED),
            (TaskState.UPLOADED, TaskState.SUCCEEDED),
        ]
        for src, dst in path:
            with self.subTest(src=src, dst=dst):
                self.assertTrue(can_transition(src, dst))

    def test_failure_returns_to_queued_not_leased(self):
        self.assertEqual(next_after_failure(TaskState.RUNNING, retries=1), TaskState.QUEUED)
        self.assertEqual(next_after_failure(TaskState.RUNNING, retries=3), TaskState.DEAD_LETTER)

    def test_terminal_states_cannot_transition(self):
        self.assertFalse(can_transition(TaskState.SUCCEEDED, TaskState.QUEUED))
        self.assertFalse(can_transition(TaskState.CANCELLED, TaskState.QUEUED))

    def test_backends_defined(self):
        self.assertEqual({b.value for b in BackendKind}, {"cloud_api", "local_comfyui", "cloud_gpu"})
        self.assertEqual(FailureCode.TRANSIENT.value, "transient")


if __name__ == "__main__":
    unittest.main()
