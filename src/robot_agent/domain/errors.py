"""One task error contract; command records remain the execution authority."""

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4


# Only these authored messages cross the API boundary. Provider/robot exception
# text stays in its source exception or recorded robot evidence, never in a view.
MESSAGES = {
    "NO_ONLINE_ROBOT": "当前没有可用的在线机器人。",
    "MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED": "有多台空闲机器人，当前不能自动选择。",
    "ROBOT_STATE_UNKNOWN": "机器人的忙闲状态缺失或已过期。",
    "CONNECTION_CHANGED": "机器人连接已变化。",
    "CANDIDATE_CUSTOMER_MISMATCH": "机器人归属校验失败。",
    "PLANNER_UNAVAILABLE": "规划服务暂时不可用。",
    "PLANNER_PROVIDER_REJECTED": "规划服务拒绝了本次请求。",
    "PLANNER_RESPONSE_TOO_LARGE": "规划服务返回内容超过限制。",
    "PLANNER_INVALID_OUTPUT": "规划服务返回的计划格式不正确。",
    "PLAN_INVALID": "计划不符合执行要求。",
    "PLAN_LIMIT_EXCEEDED": "计划步骤超过允许数量。",
    "UNSUPPORTED": "当前能力无法完成这个任务。",
    "PLANNING_INTERRUPTED": "本次规划已中断。",
    "CAPABILITY_UNKNOWN": "机器人的能力信息缺失或不可用。",
    "ACTION_UNSUPPORTED": "机器人不支持计划中的动作。",
    "WAYPOINT_UNVERIFIED": "计划中的地点缺失、已过期或未得到机器人确认。",
    "SKILL_UNVERIFIED": "计划中的技能缺失、已过期或未得到机器人确认。",
    "READINESS_UNKNOWN": "机器人所需的就绪状态缺失、未就绪或已过期。",
    "RESOURCE_VERSION_CHANGED": "计划所依据的资源已经变化。",
    "RESOURCE_EXPIRED": "本次执行所依据的机器人状态已过期。",
    "ROBOT_BINDING_UNAVAILABLE": "机器人绑定不可用。",
    "ROBOT_REPORT_INVALID": "机器人上报的状态格式不正确。",
    "RESOURCE_BINDING_DENIED": "无权使用这个机器人。",
    "AUTHORIZATION_DENY": "当前权限不允许执行这个任务。",
    "AUTHORIZATION_UNAVAILABLE": "暂时无法确认任务执行权限。",
    "AUTHORIZATION_REQUIRE_CONFIRMATION": "执行权限需要重新确认，原任务已停止推进。",
    "AUTHORIZATION_REQUIRED": "执行权限需要重新确认，原任务已停止推进。",
    "APPROVAL_VALIDITY_UNAVAILABLE": "无法确认批准的有效期限。",
    "APPROVAL_DENIED": "当前用户无权批准这个任务。",
    "APPROVAL_REJECTED": "任务计划未获批准。",
    "APPROVAL_CHANGED": "任务批准记录已经变化。",
    "BINDING_ISOLATED": "机器人当前已停用或被隔离。",
    "REVALIDATION_REQUIRED": "执行条件已经变化，原任务已停止推进。",
    "OWNERSHIP_UNAVAILABLE": "当前服务没有有效的执行权限。",
    "ALLOCATION_LOST": "任务已失去机器人的占用记录。",
    "TASK_CHANGED": "任务状态已经变化，本条指令未发送。",
    "COMMAND_NOT_DISPATCHABLE": "指令状态不允许发送。",
    "ROBOT_DISCONNECTED": "机器人连接已断开，本条指令未发送。",
    "SEND_NOT_ATTEMPTED": "本条指令未发送。",
    "SEND_FAILED": "发送指令时连接异常，是否送达尚未确认。",
    "SEND_INTERRUPTED": "指令发送已中断，是否送达尚未确认。",
    "CANCEL_NOT_SENT": "停止请求未能发送，尚未确认机器人停止。",
    "CANCEL_SEND_UNKNOWN": "停止请求是否送达尚未确认，尚未确认机器人停止。",
    "ACTION_FAILED": "机器人报告本条动作执行失败。",
    "RESULT_UNKNOWN": "已发指令的执行结果尚未确认。",
    "COMMAND_TIMEOUT": "等待机器人回执超时，执行结果尚未确认。",
    "EVIDENCE_CONFLICT": "机器人对同一条指令报告了相互矛盾的结果。",
    "UNTRACKED_EXECUTION": "机器人正在执行无法关联到当前任务的指令。",
    "RESTART_TASK_LOST": "服务重启，原任务已结束。",
    "ROBOT_DISABLED": "机器人已停用，原任务已结束。",
    "INTERNAL_ERROR": "处理任务时发生异常，原任务已停止推进。",
    "PERSISTENCE_UNCONFIRMED": "任务记录未能确认保存，请查询原请求的受理结果。",
}

ALIASES = {
    "unverified waypoint": "WAYPOINT_UNVERIFIED",
    "unverified skill": "SKILL_UNVERIFIED",
    "unknown or unsupported action": "ACTION_UNSUPPORTED",
}


def error_code(reason, fallback="INTERNAL_ERROR"):
    value = getattr(reason, "reason", str(reason))
    if value in MESSAGES:
        return value
    if value in ALIASES:
        return ALIASES[value]
    if isinstance(value, str) and value.startswith("REPORT_INVALID:"):
        return "ROBOT_REPORT_INVALID"
    return fallback


def error_source(code):
    if code in {"NO_ONLINE_ROBOT", "MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED", "ROBOT_STATE_UNKNOWN",
                "CONNECTION_CHANGED", "CANDIDATE_CUSTOMER_MISMATCH", "ALLOCATION_LOST"}:
        return "scheduling"
    if code.startswith(("AUTHORIZATION_", "APPROVAL_")) or code == "RESOURCE_BINDING_DENIED":
        return "authorization"
    if code.startswith(("PLANNER_", "PLAN_")) or code in {"UNSUPPORTED", "PLANNING_INTERRUPTED"}:
        return "planning"
    if code in {"ACTION_FAILED", "RESULT_UNKNOWN", "COMMAND_TIMEOUT", "EVIDENCE_CONFLICT", "UNTRACKED_EXECUTION"}:
        return "execution"
    if code.startswith(("SEND_", "CANCEL_")) or code == "ROBOT_DISCONNECTED":
        return "transport"
    if code in {"OWNERSHIP_UNAVAILABLE", "RESTART_TASK_LOST", "ROBOT_DISABLED", "INTERNAL_ERROR", "PERSISTENCE_UNCONFIRMED"}:
        return "runtime"
    return "resources"


def make_error(reason, *, source="", phase="", command=None, fallback="INTERNAL_ERROR"):
    code = error_code(reason, fallback)
    source = getattr(reason, "source", "") or source or error_source(code)
    phase = getattr(reason, "phase", "") or phase or {
        "scheduling": "scheduling", "planning": "planning", "resources": "dispatch",
        "authorization": "approval", "transport": "write", "execution": "feedback", "runtime": "runtime",
    }.get(source, "runtime")
    result = {"error_id": str(uuid4()), "code": code, "source": source, "message": MESSAGES[code],
              "phase": phase, "occurred_at": datetime.now(timezone.utc).isoformat()}
    if command:
        result.update(step_id=command.step_id, command_id=command.command_id)
    return result


def stop_task(task, reason=None, *, source="", phase="", command=None, state=None, fallback="INTERNAL_ERROR"):
    """Latch the first error. Later receipts only refine separate command facts."""
    error = task.error or make_error(reason or task.reason_code, source=source, phase=phase,
                                    command=command, fallback=fallback)
    return replace(task, state=state or task.state, error=error, advancement_stopped=True,
                   reason_code=task.reason_code or error["code"], resume_phase="")


def execution_status(task):
    if task.result_disputed or any(command.evidence_conflict for command in task.commands):
        return "CONFLICT"
    started = [command for command in task.commands if command.dispatch_state not in {"PREPARED", "NOT_SENT"}]
    if any(command.outcome not in {"SUCCEEDED", "FAILED"} for command in started):
        return "UNKNOWN"
    if any(command.outcome == "FAILED" for command in started):
        return "FAILED"
    succeeded = {command.step_id for command in started if command.outcome == "SUCCEEDED"}
    if task.plan and len(succeeded) == len(task.plan.steps):
        return "SUCCEEDED"
    return "PARTIAL" if succeeded else "NOT_SENT"


def public_error(task):
    return {**task.error, "execution_status": execution_status(task)} if task.error else None
