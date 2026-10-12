"""Deterministic plan grounding; model identifiers never grant capability."""

from .models import ActionCatalog, CandidatePlan, ContextSnapshot, PlanError, PlanStep, ValidationResult


def validate_plan(candidate: CandidatePlan, snapshot: ContextSnapshot, catalog: ActionCatalog) -> ValidationResult:
    if candidate.outcome != "plan" or not candidate.steps:
        raise PlanError("plan requires steps")
    seen = set()
    grounded = []
    previous = None
    for step in candidate.steps:
        if step.step_id in seen:
            raise PlanError("duplicate step id")
        # First release has a fully specified serial dependency chain.
        required_dependencies = () if previous is None else (previous,)
        if step.depends_on != required_dependencies:
            raise PlanError("invalid dependencies: serial chain required")
        if step.action not in catalog.required or step.action not in snapshot.allowed_actions or step.action not in snapshot.skills:
            raise PlanError("unknown or unsupported action")
        required = catalog.required[step.action]
        if set(step.args) != set(required):
            raise PlanError("invalid action parameters")
        if any(not isinstance(step.args[key], str) or not step.args[key].strip() for key in required):
            raise PlanError("empty or invalid action parameter")
        refs = []
        if step.action == "move_to":
            ref = snapshot.waypoints.get(step.args["location"])
            if not ref:
                raise PlanError("unverified waypoint")
            refs.append(ref)
        elif step.action == "vla_skill":
            ref = snapshot.brain_skills.get(step.args["skill_id"])
            if not ref:
                raise PlanError("unverified skill")
            refs.append(ref)
        else:
            raise PlanError("action grounding not configured")
        grounded.append(PlanStep(step.step_id, step.action, dict(step.args), step.depends_on, tuple(refs)))
        seen.add(step.step_id)
        previous = step.step_id
    return ValidationResult(tuple(grounded), snapshot.snapshot_id)
