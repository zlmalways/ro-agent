"""Deterministic plan grounding; model identifiers never grant capability."""

from .models import (
    ActionCatalog, CandidatePlan, ContextSnapshot, ModelOutputError, PlanError, PlanStep,
    PreflightBlocked, ValidationResult,
)


def validate_plan(candidate: CandidatePlan, snapshot: ContextSnapshot, catalog: ActionCatalog, *,
                  model_output: bool = False) -> ValidationResult:
    # A fresh output is checked against the exact snapshot supplied to the model.
    # Dispatch instead checks current resources, whose changes are not model errors.
    def reject(reason, feedback, *, source=""):
        if model_output:
            raise ModelOutputError("PLAN_INVALID", feedback=feedback)
        raise PlanError(reason, source=source)

    if model_output and (not snapshot.robot_binding_id or not snapshot.skills):
        raise PreflightBlocked("CAPABILITY_UNKNOWN", source="resources")
    if candidate.outcome != "plan" or not candidate.steps:
        reject("plan requires steps", "outcome=plan 时必须提供至少一个动作步骤。")
    seen = set()
    grounded = []
    previous = None
    for step in candidate.steps:
        if step.step_id in seen:
            reject("duplicate step id", f"步骤编号 {step.step_id!r} 重复，每一步的 step_id 必须唯一。")
        # First release has a fully specified serial dependency chain.
        required_dependencies = () if previous is None else (previous,)
        if step.depends_on != required_dependencies:
            reject("invalid dependencies: serial chain required",
                   f"步骤 {step.step_id!r} 的 depends_on 应为 {list(required_dependencies)!r}；步骤必须串行执行。")
        if step.action not in catalog.required:
            reject("ACTION_UNSUPPORTED", f"动作 {step.action!r} 不受支持，只能使用 {sorted(catalog.required)!r}。",
                   source="resources")
        if step.action not in snapshot.allowed_actions:
            if model_output:
                raise PreflightBlocked("ACTION_NOT_ALLOWED", source="configuration")
            raise PlanError("ACTION_UNSUPPORTED", source="resources")
        if step.action not in snapshot.skills:
            reject("ACTION_UNSUPPORTED", f"动作 {step.action!r} 不在本次提供的机器人动作清单中。",
                   source="resources")
        required = catalog.required[step.action]
        args = dict(step.args)
        # 临时修改：仅“冰箱”和“冰箱旁”的导航默认使用 manipulation；模型输出格式保持不变。
        fridge_navigation = step.action == "move_to" and args.get("location") in ("冰箱", "冰箱旁")
        fields = required
        # 已保存计划会再次校验，需接受代码补充的到达模式。
        if fridge_navigation and not model_output and "arrival_mode" in args:
            fields = (*required, "arrival_mode")
        if set(args) != set(fields):
            reject("invalid action parameters",
                   f"步骤 {step.step_id!r} 的 args 字段必须且只能包含 {list(fields)!r}。")
        if any(not isinstance(args[key], str) or not args[key].strip() for key in required):
            reject("empty or invalid action parameter", f"步骤 {step.step_id!r} 的参数值必须是非空字符串。")
        if fridge_navigation and "arrival_mode" in args and args["arrival_mode"] not in ("standard", "manipulation"):
            reject("invalid arrival mode", f"步骤 {step.step_id!r} 的 arrival_mode 必须是 standard 或 manipulation。")
        refs = []
        if step.action == "move_to":
            ref = snapshot.waypoints.get(step.args["location"])
            if not ref:
                reject("WAYPOINT_UNVERIFIED",
                       f"地点 {step.args['location']!r} 不在本次提供的可用地点清单中，请使用准确名称；信息不足时澄清。",
                       source="resources")
            refs.append(ref)
            if fridge_navigation:
                args.setdefault("arrival_mode", "manipulation")
        elif step.action == "vla_skill":
            ref = snapshot.brain_skills.get(step.args["skill_id"])
            if not ref:
                reject("SKILL_UNVERIFIED",
                       f"技能 {step.args['skill_id']!r} 不在本次提供的可用技能清单中，不得编造；无法完成时返回 unsupported。",
                       source="resources")
            refs.append(ref)
        else:
            raise PreflightBlocked("ACTION_GROUNDING_UNAVAILABLE", source="configuration")
        grounded.append(PlanStep(step.step_id, step.action, args, step.depends_on, tuple(refs)))
        seen.add(step.step_id)
        previous = step.step_id
    return ValidationResult(tuple(grounded), snapshot.snapshot_id)
