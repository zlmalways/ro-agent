from dataclasses import replace

import pytest

from robot_agent.application.scheduling import select_single_candidate as schedule
from robot_agent.domain.models import TaskInput, TrustedContext
from tests.fixtures.minimal import TestResources as FixtureResources


@pytest.mark.parametrize("count", [0, 1, 2, 10])
async def test_candidate_count_returns_choice_or_reason_without_mutation(count):
    snapshot = await FixtureResources().snapshot(TrustedContext("test-customer", "user", "test-user"))
    candidates = [replace(snapshot, robot_binding_id=f"robot-{i}") for i in range(count)]
    original = list(candidates)
    result = schedule(TaskInput("r", "web", "去桌边", ""), candidates)
    assert candidates == original
    if count == 1:
        assert result.snapshot is candidates[0] and not result.reason_code
    else:
        assert result.snapshot is None
        assert result.reason_code == ("NO_ONLINE_ROBOT" if count == 0 else "MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED")


async def test_calls_are_independent_and_choice_is_not_execution_permission():
    snapshot = await FixtureResources().snapshot(TrustedContext("test-customer", "user", "test-user"))
    task = TaskInput("r", "web", "去桌边", "")
    not_ready = replace(snapshot, readiness={}, skills=frozenset())
    assert schedule(task, [not_ready]).snapshot is not_ready
    assert schedule(task, []).snapshot is None
    other = replace(snapshot, robot_binding_id="other")
    assert schedule(task, [other]).snapshot is other
    assert schedule(task, [other, snapshot]).snapshot is None
