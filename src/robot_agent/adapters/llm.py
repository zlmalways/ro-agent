import asyncio
import json
import logging
from dataclasses import replace
from uuid import uuid4

import httpx

from robot_agent.domain.models import CandidatePlan, DependencyUnavailable, ModelOutputError, PlanError


log = logging.getLogger(__name__)


PLANNING_RULES = (
    "你是机器人任务会话中的助手，可以自然对话，也可以规划机器人任务。请根据完整会话历史和当前机器人资料理解用户最新要求。"
    "你只能返回一个 JSON 对象，自然语言写在对应字段里，不要在 JSON 外输出文字、工具调用或视觉内容。"
    "问候、普通咨询、解释和无需机器人动作的交流返回 outcome=reply、text；text 是直接给用户的自然语言回复，不能为空。"
    "不要把普通问候或咨询当成缺少任务信息的澄清，不要为普通回复生成动作或声称机器人已经执行。"
    "需要执行时返回 outcome=plan 和 steps；步骤必须按顺序执行，第一步 depends_on 为空，后续步骤只依赖前一步。"
    "move_to 的 args 必须是 {location}，vla_skill 的 args 必须是 {skill_id}，只能使用资料中出现的地点和技能标识。"
    "只有用户要求执行机器人任务，但必要信息不足或需要用户选择时，才返回 outcome=clarify、question、required_slots，不能返回步骤。"
    "能力不支持时返回 outcome=unsupported、reason，不能返回步骤。"
    "所有结果包含 outcome、text、steps、question、required_slots、reason。reply 的 steps、required_slots 为空数组，question、reason 为空字符串；其他结果的 text 为空字符串。"
    "不要生成 customer_id、approval、source_refs、validation、工具调用或执行结果字段。"
)


def _object_schema(**properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_PLAN_SCHEMA = _object_schema(
    outcome={"type": "string", "enum": ["plan", "clarify", "unsupported", "reply"]},
    text=_STRING,
    steps={"type": "array", "items": _object_schema(
        step_id=_STRING,
        action={"type": "string", "enum": ["move_to", "vla_skill"]},
        args={"anyOf": [_object_schema(location=_STRING), _object_schema(skill_id=_STRING)]},
        depends_on=_STRINGS,
    )},
    question=_STRING,
    required_slots=_STRINGS,
    reason=_STRING,
)


def _responses_text(body):
    if body.get("status") != "completed" or body.get("error") or not isinstance(body.get("output"), list):
        raise ValueError("incomplete response")
    parts = []
    for item in body["output"]:
        if item["type"] == "reasoning":
            continue
        if (item["type"] != "message" or item.get("role") != "assistant"
                or item.get("status", "completed") != "completed"):
            raise ValueError("unexpected output")
        for block in item["content"]:
            if block["type"] != "output_text":
                raise ValueError("expected text")
            parts.append(block["text"])
    return "".join(parts)


class OpenAICompatiblePlanner:
    def __init__(self, config, *, client=None, max_response_bytes=65536, history_loader=None,
                 audit_store=None):
        self.config = config
        self.client = client
        self.max_response_bytes = max_response_bytes
        self.history_loader = history_loader
        self.audit_store = audit_store

    async def call_llm(self, conversation_id, snapshot, *, turn_id=None, repair=None):
        if self.history_loader is None:
            raise DependencyUnavailable("PLANNER_HISTORY_UNAVAILABLE")
        try:
            messages = await self.history_loader(snapshot.customer_id, conversation_id)
        except Exception as exc:
            raise DependencyUnavailable("PLANNER_HISTORY_UNAVAILABLE") from exc
        return await self._request(conversation_id, messages, snapshot, turn_id=turn_id, repair=repair)

    async def _request(self, conversation_id, messages, snapshot, *, turn_id=None, repair=None):
        if not self.config or not self.config.api_key or not self.config.model or not self.config.base_url:
            raise DependencyUnavailable("PLANNER_CONFIG_MISSING", source="configuration")
        context = json.dumps({
            "task_id": conversation_id,
            "robot_context": {
                "skills": sorted(snapshot.skills),
                "waypoints": sorted(snapshot.waypoints),
                "brain_skills": sorted(snapshot.brain_skills),
                "capability_labels": dict(snapshot.capability_labels),
                "environment_labels": dict(snapshot.environment_labels),
            },
        }, ensure_ascii=False)
        system_prompt = PLANNING_RULES + "\n当前机器人资料：" + context
        history = [
            {"role": row["role"], "content": row["content"]}
            for row in messages if row.get("role") in {"user", "assistant"} and row.get("content")
        ]
        if not history:
            raise DependencyUnavailable("PLANNER_EMPTY_CONTEXT", source="storage")
        if repair is not None:
            # Private agent context only: neither fragment is saved as a user/chat message.
            if repair.previous_output:
                history.append({"role": "assistant", "content": repair.previous_output})
            system_prompt += (
                "\n上一轮输出未通过程序校验，尚未执行任何动作。请根据原用户要求和原机器人资料重新返回完整 JSON。"
                "修正下列错误，不要解释纠错过程或声称动作已执行；信息不足可澄清，能力不足可返回 unsupported。"
                "\n系统校验反馈：" + json.dumps({"error": repair.feedback}, ensure_ascii=False)
            )
        relay = self.config.name == "relay"
        if relay:
            path = "/responses"
            request = {
                "model": self.config.model,
                "instructions": system_prompt,
                "input": [{"role": item["role"], "content": [{
                    "type": "output_text" if item["role"] == "assistant" else "input_text",
                    "text": item["content"]
                }]} for item in history],
                "text": {"format": {"type": "json_schema", "name": "robot_task_plan",
                                    "strict": True, "schema": _PLAN_SCHEMA}},
                "max_output_tokens": 5000, "store": False,
            }
        else:
            path = "/chat/completions"
            request = {
                "model": self.config.model,
                "messages": [{"role": "system", "content": system_prompt}, *history],
                "temperature": 0, "response_format": {"type": "json_object"},
            }

        url = self.config.base_url.rstrip("/") + path
        headers = {"Authorization": "Bearer " + self.config.api_key}
        audit_endpoint = url.replace(self.config.api_key, "[redacted]") if self.config.api_key else url
        llm_call_id = None
        result = {}

        async def finish_audit(*, status, response_json=None, response_text=None,
                               http_status=None, provider_request_id=None):
            if llm_call_id is not None:
                await self.audit_store.finish_llm_call(
                    snapshot.customer_id,
                    llm_call_id,
                    status=status,
                    response_json=response_json,
                    response_text=response_text,
                    http_status=http_status,
                    provider_request_id=provider_request_id,
                )

        try:
            if self.audit_store is not None:
                if not turn_id:
                    raise DependencyUnavailable("PLANNER_AUDIT_UNAVAILABLE")
                llm_call_id = str(uuid4())
                await self.audit_store.start_llm_call(
                    customer_id=snapshot.customer_id,
                    llm_call_id=llm_call_id,
                    turn_id=turn_id,
                    provider=self.config.name,
                    model=self.config.model,
                    endpoint=audit_endpoint,
                    request_json=request,
                )

            try:
                if self.client is None:
                    async with httpx.AsyncClient(timeout=self.config.timeout_seconds, trust_env=not relay) as client:
                        response = await client.post(url, headers=headers, json=request, follow_redirects=False)
                else:
                    response = await self.client.post(url, headers=headers, json=request, follow_redirects=False)
            except httpx.HTTPError as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if self.config.api_key:
                    detail = detail.replace(self.config.api_key, "[redacted]")
                result = {"response_text": detail}
                await finish_audit(status="FAILED", **result)
                code = "PLANNER_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "PLANNER_CONNECTION_FAILED"
                raise DependencyUnavailable(code, source="planner_call") from exc

            response_json = None
            response_text = None
            try:
                response_json = response.json()
            except (TypeError, ValueError):
                response_text = response.text
            # A JSON null response still needs to be distinguishable from no response.
            if response_json is None and response_text is None:
                response_text = response.text
            provider_request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            if not provider_request_id and isinstance(response_json, dict):
                body_id = response_json.get("id")
                if isinstance(body_id, str):
                    provider_request_id = body_id
            result = dict(response_json=response_json, response_text=response_text,
                          http_status=response.status_code, provider_request_id=provider_request_id)

            try:
                if response.status_code in {401, 403}:
                    raise DependencyUnavailable("PLANNER_AUTH_FAILED", source="configuration")
                if response.status_code == 429:
                    raise DependencyUnavailable("PLANNER_RATE_LIMITED", source="planner_call")
                if response.status_code >= 500:
                    raise DependencyUnavailable("PLANNER_UNAVAILABLE", source="planner_call")
                if response.status_code >= 300:
                    raise DependencyUnavailable("PLANNER_PROVIDER_REJECTED", source="planner_call")
                if len(response.content) > self.max_response_bytes:
                    raise DependencyUnavailable("PLANNER_RESPONSE_TOO_LARGE", source="planner_call")
                try:
                    content = (_responses_text(response_json) if relay
                               else response_json["choices"][0]["message"]["content"])
                    if not isinstance(content, str):
                        raise ValueError("model message is not text")
                except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
                    raise DependencyUnavailable("PLANNER_INVALID_RESPONSE", source="planner_call") from exc
                try:
                    value = json.loads(content)
                except ValueError as exc:
                    raise ModelOutputError("PLANNER_INVALID_OUTPUT", feedback=f"输出必须是一个完整的 JSON 对象：{exc}",
                                           raw_output=content, llm_call_id=llm_call_id) from exc
                try:
                    candidate = CandidatePlan.from_dict(value)
                except PlanError as exc:
                    raise ModelOutputError("PLANNER_INVALID_OUTPUT", feedback=f"输出字段或类型不符合要求：{exc}",
                                           raw_output=content, llm_call_id=llm_call_id) from exc
            except (DependencyUnavailable, PlanError):
                await finish_audit(status="FAILED", **result)
                raise
            await finish_audit(status="SUCCEEDED", **result)
            return replace(candidate, model_output=content, llm_call_id=llm_call_id)

        except asyncio.CancelledError:
            if llm_call_id is not None:
                # Keep the short audit write alive if shutdown cancels this work again.
                cleanup = asyncio.create_task(self.audit_store.cancel_llm_call(
                    snapshot.customer_id, llm_call_id, **result))
                try:
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
                except Exception as exc:
                    log.error("llm cancellation audit failed: llm_call_id=%s error_type=%s",
                              llm_call_id, type(exc).__name__)
            raise

    async def plan(self, goal, answers, snapshot):
        """Legacy adapter kept for old callers; new task flow uses call_llm."""
        context = json.dumps({
            "goal": goal, "answers": answers,
            "skills": sorted(snapshot.skills), "waypoints": sorted(snapshot.waypoints),
            "brain_skills": sorted(snapshot.brain_skills),
            "capability_labels": dict(snapshot.capability_labels),
            "environment_labels": dict(snapshot.environment_labels),
        }, ensure_ascii=False)
        return await self._request("", [{"role": "user", "content": context}], snapshot)
