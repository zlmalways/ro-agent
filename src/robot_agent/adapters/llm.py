import json

import httpx

from robot_agent.domain.models import CandidatePlan, DependencyUnavailable, PlanError


class OpenAICompatiblePlanner:
    def __init__(self, config, *, client=None, max_response_bytes=65536):
        self.config = config
        self.client = client
        self.max_response_bytes = max_response_bytes

    async def plan(self, goal, answers, snapshot):
        if not self.config or not self.config.api_key or not self.config.model or not self.config.base_url:
            raise DependencyUnavailable("PLANNER_UNAVAILABLE")
        request = {"model": self.config.model,
                   "messages": [{"role": "system", "content":
                                 "Generate a complete serial robot plan as JSON. Use outcome=plan with steps=[{step_id,action,args,depends_on}]; "
                                 "the first step has no dependencies and each next step depends on the previous one. "
                                 "move_to requires {location}, vla_skill requires {skill_id}, using only provided IDs. "
                                 "For ambiguous input use outcome=clarify, question and required_slots, no steps. "
                                 "For unsupported input use outcome=unsupported, reason, no steps. "
                                 "Never include customer_id, approval, source_refs, or validation fields."},
                                {"role": "user", "content": json.dumps({"goal": goal, "answers": answers,
                                                                           "skills": sorted(snapshot.skills),
                                                                           "waypoints": sorted(snapshot.waypoints),
                                                                           "brain_skills": sorted(snapshot.brain_skills)},
                                                                          ensure_ascii=False)}],
                   "temperature": 0, "response_format": {"type": "json_object"}}
        # Labels are supplied by trusted, versioned documents; credential and evidence IDs stay outside the prompt.
        content = json.loads(request["messages"][-1]["content"])
        content.update(capability_labels=dict(snapshot.capability_labels), environment_labels=dict(snapshot.environment_labels))
        request["messages"][-1]["content"] = json.dumps(content, ensure_ascii=False)
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                    response = await client.post(self.config.base_url.rstrip("/") + "/chat/completions",
                                                 headers={"Authorization": "Bearer " + self.config.api_key}, json=request)
            else:
                response = await self.client.post(self.config.base_url.rstrip("/") + "/chat/completions",
                                                  headers={"Authorization": "Bearer " + self.config.api_key}, json=request)
        except httpx.HTTPError as exc:
            raise DependencyUnavailable("PLANNER_UNAVAILABLE") from exc
        if response.status_code >= 500:
            raise DependencyUnavailable("PLANNER_UNAVAILABLE")
        if response.status_code >= 400:
            raise PlanError("PLANNER_PROVIDER_REJECTED")
        if len(response.content) > self.max_response_bytes:
            raise PlanError("PLANNER_RESPONSE_TOO_LARGE")
        try:
            content = response.json()["choices"][0]["message"]["content"]
            return CandidatePlan.from_dict(json.loads(content))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlanError("PLANNER_INVALID_OUTPUT") from exc
