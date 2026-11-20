"""任务状态机。

created → queued → leased → running → uploaded → succeeded
失败统一回到 queued 重新派单（不回 leased，避免再次绑定坏节点）；
重试 3 次进 dead_letter（人工介入）；另有 cancelled 终态。
所有状态迁移写 task_events。后端类型变化时状态机不变。
"""

from __future__ import annotations

from enum import Enum


class TaskState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    UPLOADED = "uploaded"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


TERMINAL_STATES = {TaskState.SUCCEEDED, TaskState.DEAD_LETTER, TaskState.CANCELLED}

MAX_RETRIES = 3

# 合法迁移表
_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.QUEUED: {TaskState.LEASED, TaskState.CANCELLED},
    TaskState.LEASED: {TaskState.RUNNING, TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.RUNNING: {TaskState.UPLOADED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.UPLOADED: {TaskState.SUCCEEDED, TaskState.FAILED},
    TaskState.FAILED: {TaskState.QUEUED, TaskState.DEAD_LETTER},
}


class InvalidTransition(ValueError):
    pass


def can_transition(src: TaskState, dst: TaskState) -> bool:
    if src in TERMINAL_STATES:
        return False
    return dst in _TRANSITIONS.get(src, set())


def next_after_failure(state: TaskState, retries: int) -> TaskState:
    """失败后的下一个状态：未超重试上限回 queued，否则进死信。"""
    if retries >= MAX_RETRIES:
        return TaskState.DEAD_LETTER
    return TaskState.QUEUED
