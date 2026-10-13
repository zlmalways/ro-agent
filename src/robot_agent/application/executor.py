"""Compute execution changes; TaskService alone owns their transaction."""

from dataclasses import replace
from uuid import uuid4

from robot_agent.domain.models import ChangeSet, Command
from robot_agent.domain.transitions import check_transition


class Executor:
    def prepare(self, context, unit, facts):
        task = facts
        if task.state not in {"READY", "QUEUED", "RUNNING"} or task.result_disputed:
            return ChangeSet(task)
        if any(item.outcome not in {"SUCCEEDED", "FAILED"} for item in task.commands):
            return ChangeSet(task)
        remaining = [step for step in task.plan.steps if step.step_id not in task.completed]
        if not remaining:
            return ChangeSet(task)
        step = remaining[0]
        if any(dep not in task.completed for dep in step.depends_on):
            return ChangeSet(task)
        command = Command(str(uuid4()), task.task_id, task.customer_id, task.unit_id, task.plan.plan_id,
                          step.step_id, task.snapshot.robot_binding_id, step.action, dict(step.args),
                          task.snapshot.generation)
        return ChangeSet(replace(task, state="RUNNING", commands=task.commands + (command,), version=task.version + 1), command)

    def apply_event(self, facts, event, *, current_generation=None):
        command = next((item for item in facts.commands if item.command_id == event.cmd_id), None)
        return check_transition(facts, command, event, current_generation=current_generation)
