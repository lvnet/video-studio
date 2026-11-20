"""骨架冒烟测试：验证状态机与八步链定义一致。"""

from video_studio.compute.base import BackendKind, FailureCode
from video_studio.control.state import (
    TaskState,
    can_transition,
    next_after_failure,
)
from video_studio.creation.pipeline import PIPELINE, Step, next_step


def test_pipeline_has_eight_steps():
    assert len(PIPELINE) == 8
    assert PIPELINE[0] == Step.IDEA
    assert PIPELINE[-1] == Step.COMPOSE


def test_next_step():
    assert next_step(Step.IDEA) == Step.SCRIPT
    assert next_step(Step.COMPOSE) is None


def test_state_machine_happy_path():
    path = [
        (TaskState.CREATED, TaskState.QUEUED),
        (TaskState.QUEUED, TaskState.LEASED),
        (TaskState.LEASED, TaskState.RUNNING),
        (TaskState.RUNNING, TaskState.UPLOADED),
        (TaskState.UPLOADED, TaskState.SUCCEEDED),
    ]
    for src, dst in path:
        assert can_transition(src, dst)


def test_failure_returns_to_queued_not_leased():
    assert next_after_failure(TaskState.RUNNING, retries=1) == TaskState.QUEUED
    assert next_after_failure(TaskState.RUNNING, retries=3) == TaskState.DEAD_LETTER


def test_terminal_states_cannot_transition():
    assert not can_transition(TaskState.SUCCEEDED, TaskState.QUEUED)
    assert not can_transition(TaskState.CANCELLED, TaskState.QUEUED)


def test_backends_defined():
    assert {b.value for b in BackendKind} == {
        "cloud_api", "local_comfyui", "cloud_gpu"
    }
    assert FailureCode.TRANSIENT.value == "transient"
