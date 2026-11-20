"""Seed CRUD service. One deliberate regression is marked for agent testing."""
from __future__ import annotations


class TaskBoard:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, object]] = {}

    def create_task(self, task_id: str, title: str) -> dict[str, object]:
        if task_id in self.tasks:
            raise ValueError("Task already exists")
        task: dict[str, object] = {"id": task_id, "title": title, "done": False}
        self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> dict[str, object]:
        return self.tasks[task_id]

    def complete_task(self, task_id: str) -> dict[str, object]:
        task = self.get_task(task_id)
        task["done"] = False  # BUG: should become True
        return task
