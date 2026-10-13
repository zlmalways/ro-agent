import httpx
import pytest

from tests.api.test_tasks import HEADERS, api_app


pytestmark = pytest.mark.integration


async def test_clarification_keeps_task_and_rejects_other_speaker(persisted_service):
    service, robot = persisted_service
    service.planner.value = {"outcome": "clarify", "question": "Which table?", "required_slots": ["location"]}
    app = api_app(service)
    app.state.identity.users["test-stranger"] = {"customer_id": "test-customer", "actor_id": "other", "roles": ["user"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        view = (await client.post("/api/robot-agent/tasks", json={"text": "test"}, headers=HEADERS)).json()
        assert view["state"] == "NEEDS_CLARIFICATION"
        service.planner.value = {"outcome": "plan", "steps": [{"step_id": "s1", "action": "move_to", "args": {"location": "test-table"}, "depends_on": []}]}
        path = f"/api/robot-agent/tasks/{view['task_id']}/clarifications/{view['clarification_id']}/answer"
        rejected = await client.post(path, json={"answer": "test-table", "expected_version": view["version"]},
                                     headers={"Authorization": "Bearer test-stranger", "Idempotency-Key": "answer-key"})
        assert rejected.status_code == 404
        result = await client.post(path, json={"answer": "test-table", "expected_version": view["version"]},
                                   headers={**HEADERS, "Idempotency-Key": "answer-key"})
        assert result.status_code == 202
        assert result.json()["task_id"] == view["task_id"]
        assert result.json()["state"] == "READY"
        assert robot.sent == []


async def test_approval_endpoint_rejects_wrong_plan_and_unauthorized_user(persisted_service):
    service, robot = persisted_service
    service.policy.result = "require_confirmation"
    app = api_app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        view = (await client.post("/api/robot-agent/tasks", json={"text": "test"}, headers=HEADERS)).json()
        request = {"plan_id": "other-plan", "decision": "approve", "expected_version": view["version"]}
        response = await client.post(f"/api/robot-agent/tasks/{view['task_id']}/decisions", json=request, headers=HEADERS)
        assert response.status_code == 409
        async def cannot_approve(context, task):
            return False
        service.policy.can_approve = cannot_approve
        request["plan_id"] = view["plan_id"]
        denied = await client.post(f"/api/robot-agent/tasks/{view['task_id']}/decisions", json=request, headers=HEADERS)
        assert denied.status_code == 403
        assert robot.sent == []


async def test_provider_unavailable_returns_503_and_persistent_block(persisted_service, postgres_store):
    from robot_agent.adapters.llm import OpenAICompatiblePlanner
    service, robot = persisted_service
    service.planner = OpenAICompatiblePlanner(None, client=None, max_response_bytes=4096)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(api_app(service)), base_url="http://test") as client:
        response = await client.post("/api/robot-agent/tasks", json={"text": "test"}, headers=HEADERS)
        assert response.status_code == 503
        assert await postgres_store.pool.fetchval("SELECT state FROM ra_tasks") == "BLOCKED"
        assert robot.sent == []
