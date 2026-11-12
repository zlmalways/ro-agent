"""Compute execution changes; TaskService alone owns their transaction."""

from dataclasses import replace
from uuid import uuid4

from robot_agent.domain.models import ChangeSet, Command
from robot_agent.domain.transitions import check_transition, remaining_steps


class Executor:
    def prepare(self, context, unit, facts):
        task = facts
        if task.state not in {"READY", "QUEUED", "RUNNING"} or task.result_disputed or task.advancement_stopped:
            return ChangeSet(task)
        if any(item.step_id not in task.skipped_steps and item.outcome not in {"SUCCEEDED", "FAILED"}
               for item in task.commands):
            return ChangeSet(task)
        remaining = remaining_steps(task)
        if not remaining:
            return ChangeSet(task)
        step = remaining[0]
        # Skips are explicit user decisions recorded by resume_execution; they
        # satisfy sequence ordering without fabricating a successful outcome.
        if any(dep not in task.completed and dep not in task.skipped_steps for dep in step.depends_on):
            return ChangeSet(task)
        command = Command(str(uuid4()), task.task_id, task.customer_id, task.unit_id, task.plan.plan_id,
                          step.step_id, task.snapshot.robot_binding_id, step.action, dict(step.args),
                          task.snapshot.generation)
        return ChangeSet(replace(task, state="RUNNING", commands=task.commands + (command,), version=task.version + 1), command)

    def apply_event(self, facts, event, *, current_generation=None):
        command = next((item for item in facts.commands if item.command_id == event.cmd_id), None)
        return check_transition(facts, command, event, current_generation=current_generation)
