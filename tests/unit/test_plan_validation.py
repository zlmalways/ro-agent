"""Behavior fixtures: deliberately fabricated test capabilities, never deployment data."""

import pytest

from robot_agent.domain.models import ActionCatalog, CandidatePlan, ContextSnapshot, PlanError
from robot_agent.domain.validation import validate_plan


def snapshot():
    return ContextSnapshot(
        customer_id="test-customer", robot_binding_id="test-binding", generation=1,
        skills=frozenset({"move_to", "vla_skill"}),
        waypoints={"test-table": "environment:v1:table"},
        brain_skills={"test-pick": "capabilities:v1:pick"},
        readiness={"nav_server_ready": True, "brain_server_ready": True, "brain_engine": "ready"},
        snapshot_id="test-snapshot", allowed_actions=frozenset({"move_to", "vla_skill"}),
    )


def candidate(steps):
    return CandidatePlan.from_dict({"outcome": "plan", "steps": steps})


def test_validated_steps_have_server_generated_evidence():
    result = validate_plan(candidate([
        {"step_id": "s1", "action": "move_to", "args": {"location": "test-table"}, "depends_on": []},
        {"step_id": "s2", "action": "vla_skill", "args": {"skill_id": "test-pick"}, "depends_on": ["s1"]},
    ]), snapshot(), ActionCatalog({"move_to": ("location",), "vla_skill": ("skill_id",)}))
    assert result.steps[0].source_refs == ("environment:v1:table",)
    assert result.steps[1].source_refs == ("capabilities:v1:pick",)


@pytest.mark.parametrize("step", [
    {"step_id": "s1", "action": "grasp", "args": {}, "depends_on": []},
    {"step_id": "s1", "action": "move_to", "args": {"location": "imaginary"}, "depends_on": []},
    {"step_id": "s1", "action": "vla_skill", "args": {"skill_id": "imaginary"}, "depends_on": []},
    {"step_id": "s1", "action": "move_to", "args": {"location": "test-table", "customer_id": "other"}, "depends_on": []},
])
def test_unsupported_or_unverified_action_cannot_be_validated(step):
    with pytest.raises(PlanError):
        validate_plan(candidate([step]), snapshot(), ActionCatalog({"move_to": ("location",), "vla_skill": ("skill_id",)}))


def test_cyclic_dependencies_are_rejected():
    with pytest.raises(PlanError, match="depend"):
        validate_plan(candidate([
            {"step_id": "a", "action": "move_to", "args": {"location": "test-table"}, "depends_on": ["b"]},
            {"step_id": "b", "action": "vla_skill", "args": {"skill_id": "test-pick"}, "depends_on": ["a"]},
        ]), snapshot(), ActionCatalog({"move_to": ("location",), "vla_skill": ("skill_id",)}))


@pytest.mark.parametrize("field", ["source_refs", "customer_id", "approval", "validation_result"])
def test_candidate_cannot_supply_trusted_fields(field):
    with pytest.raises(PlanError):
        CandidatePlan.from_dict({"outcome": "plan", "steps": [], field: "forged"})


def test_clarification_does_not_contain_action_steps():
    with pytest.raises(PlanError):
        CandidatePlan.from_dict({"outcome": "clarify", "question": "Which table?", "steps": [
            {"step_id": "a", "action": "move_to", "args": {"location": "test-table"}, "depends_on": []},
        ]})
