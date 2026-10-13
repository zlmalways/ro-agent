from datetime import datetime, timedelta, timezone

import pytest

from robot_agent.application.preflight import approval_covers
from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.adapters.policy import ConfiguredPolicy
from robot_agent.domain.models import CandidatePlan, CandidateStep, TrustedContext


NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
EVIDENCE = {"customer_id": "c", "plan_id": "p", "plan_revision": 2, "scope": ["s1", "s2"],
            "policy_version": "policy:v1", "issued_at": NOW - timedelta(minutes=1),
            "expires_at": NOW + timedelta(minutes=5), "revoked_at": None}


def test_approval_is_plan_scoped():
    assert approval_covers(EVIDENCE, customer_id="c", plan_id="p", revision=2, scope=("s1",), policy_version="policy:v1", now=NOW)
    assert not approval_covers(EVIDENCE, customer_id="c", plan_id="other", revision=2, scope=("s1",), policy_version="policy:v1", now=NOW)


@pytest.mark.parametrize("changed", [
    {"customer_id": "other"}, {"plan_revision": 3}, {"scope": ["s2"]}, {"policy_version": "old"},
    {"expires_at": NOW}, {"issued_at": NOW + timedelta(seconds=1)}, {"revoked_at": NOW},
])
def test_invalid_approval_never_covers(changed):
    assert not approval_covers({**EVIDENCE, **changed}, customer_id="c", plan_id="p", revision=2,
                               scope=("s1",), policy_version="policy:v1", now=NOW)


@pytest.mark.parametrize("rule,expected", [("allow", "allow"), ("deny", "deny"),
                                          ("require_confirmation", "require_confirmation"), (None, "unavailable")])
async def test_policy_reads_current_credential_identity(rule, expected):
    identity = ConfiguredIdentity(users={"test-token": {"customer_id": "c", "actor_id": "u", "roles": ["user"]}}, robots={})
    policy = ConfiguredPolicy(identity, version="policy:v1", actions={"c": {"move_to": rule}}, approval_ttl_seconds=300)
    candidate = CandidatePlan("plan", (CandidateStep("s1", "move_to", {"location": "table"}, ()),))
    assert await policy.evaluate(TrustedContext("c", "user", "u"), candidate, []) == expected
    identity.users.clear()
    assert await policy.evaluate(TrustedContext("c", "user", "u", frozenset({"admin"})), candidate, []) == "unavailable"


async def test_caller_roles_do_not_forge_approver():
    identity = ConfiguredIdentity(users={"test-token": {"customer_id": "c", "actor_id": "u", "roles": ["user"]}}, robots={})
    policy = ConfiguredPolicy(identity, version="policy:v1", actions={}, approval_ttl_seconds=300)
    from robot_agent.domain.models import Task
    assert not await policy.can_approve(TrustedContext("c", "user", "u", frozenset({"approver"})), Task("t", "c", "user", "u", "test"))
