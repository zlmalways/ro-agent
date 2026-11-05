import json

import httpx

from robot_agent.domain.models import CandidatePlan, DependencyUnavailable, PlanError


PLANNING_RULES = (
    "Generate a complete serial robot plan as JSON. Use outcome=plan with steps=[{step_id,action,args,depends_on}]; "
    "the first step has no dependencies and each next step depends on the previous one. "
    "move_to requires {location}, vla_skill requires {skill_id}, using only provided IDs. "
    "For ambiguous input use outcome=clarify, question and required_slots, no steps. "
    "For unsupported input use outcome=unsupported, reason, no steps. "
    "Never include customer_id, approval, source_refs, or validation fields."
)


def _object_schema(**properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_PLAN_SCHEMA = _object_schema(
    outcome={"type": "string", "enum": ["plan", "clarify", "unsupported"]},
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
    def __init__(self, config, *, client=None, max_response_bytes=65536):
        self.config = config
        self.client = client
        self.max_response_bytes = max_response_bytes

    async def plan(self, goal, answers, snapshot):
        if not self.config or not self.config.api_key or not self.config.model or not self.config.base_url:
            raise DependencyUnavailable("PLANNER_UNAVAILABLE")
        context = json.dumps({
            "goal": goal, "answers": answers, "skills": sorted(snapshot.skills),
            "waypoints": sorted(snapshot.waypoints), "brain_skills": sorted(snapshot.brain_skills),
            "capability_labels": dict(snapshot.capability_labels),
            "environment_labels": dict(snapshot.environment_labels),
        }, ensure_ascii=False)
        relay = self.config.name == "relay"
        if relay:
            path = "/responses"
            request = {
                "model": self.config.model,
                "instructions": PLANNING_RULES + " Use empty strings or arrays for fields that do not apply.",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": context}]}],
                "text": {"format": {"type": "json_schema", "name": "robot_task_plan",
                                    "strict": True, "schema": _PLAN_SCHEMA}},
                "max_output_tokens": 5000, "store": False,
            }
        else:
            path = "/chat/completions"
            request = {
                "model": self.config.model,
                "messages": [{"role": "system", "content": PLANNING_RULES}, {"role": "user", "content": context}],
                "temperature": 0, "response_format": {"type": "json_object"},
            }

        url = self.config.base_url.rstrip("/") + path
        headers = {"Authorization": "Bearer " + self.config.api_key}
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=self.config.timeout_seconds, trust_env=not relay) as client:
                    response = await client.post(url, headers=headers, json=request, follow_redirects=False)
            else:
                response = await self.client.post(url, headers=headers, json=request, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise DependencyUnavailable("PLANNER_UNAVAILABLE") from exc
        if response.status_code >= 500:
            raise DependencyUnavailable("PLANNER_UNAVAILABLE")
        if response.status_code >= 300:
            raise PlanError("PLANNER_PROVIDER_REJECTED")
        if len(response.content) > self.max_response_bytes:
            raise PlanError("PLANNER_RESPONSE_TOO_LARGE")
        try:
            body = response.json()
            content = _responses_text(body) if relay else body["choices"][0]["message"]["content"]
            return CandidatePlan.from_dict(json.loads(content))
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            raise PlanError("PLANNER_INVALID_OUTPUT") from exc
