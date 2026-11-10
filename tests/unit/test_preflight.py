from datetime import datetime, timedelta, timezone

import pytest

from robot_agent.application.preflight import Preflight
from robot_agent.domain.models import ActionCatalog, CandidatePlan, ContextSnapshot, PreflightBlocked, TrustedContext


CONTEXT = TrustedContext("test-customer", "user", "test-user")
CATALOG = ActionCatalog({"move_to": ("location",)})


class AllowPolicy:
    version = "test-policy:v1"

    async def evaluate(self, context, candidate, approvals):
        return "allow"


def expired_snapshot():
    return ContextSnapshot(
        customer_id="test-customer",
        robot_binding_id="test-binding",
        generation=1,
        skills=frozenset({"move_to"}),
        waypoints={"test-table": "robot-waypoint:test-table"},
        brain_skills={},
        readiness={"nav_server_ready": True},
        snapshot_id="expired-snapshot",
        allowed_actions=frozenset({"move_to"}),
        valid_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )


def move_plan():
    return CandidatePlan.from_dict({
        "outcome": "plan",
        "steps": [{
            "step_id": "s1",
            "action": "move_to",
            "args": {"location": "test-table"},
            "depends_on": [],
        }],
    })


@pytest.mark.asyncio
async def test_expired_snapshot_is_allowed_during_planning():
    result = await Preflight(CATALOG, AllowPolicy()).check(
        CONTEXT, move_plan(), expired_snapshot(), phase="planning")

    assert result.authorization == "allow"


@pytest.mark.asyncio
async def test_expired_snapshot_still_blocks_dispatch():
    with pytest.raises(PreflightBlocked, match="RESOURCE_EXPIRED") as error:
        await Preflight(CATALOG, AllowPolicy()).check(
            CONTEXT, move_plan(), expired_snapshot(), phase="dispatch")

    assert error.value.phase == "dispatch"
