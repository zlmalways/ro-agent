import httpx
import pytest

from robot_agent.adapters.identity import ConfiguredIdentity
from tests.api.test_tasks import api_app


@pytest.mark.integration
async def test_history_scopes_orders_and_pages(persisted_service):
    service, _ = persisted_service
    service.planner.value = {"outcome": "clarify", "question": "Which table?", "required_slots": ["location"]}
    app = api_app(service)
    users = {key: {"customer_id": tenant, "actor_id": actor, "roles": ["user"]}
             for key, tenant, actor in [("mine", "test-customer", "one"), ("peer", "test-customer", "two"),
                                        ("other", "other-customer", "one")]}
    app.state.identity = ConfiguredIdentity(users=users, robots={})
    path = "/api/robot-agent/tasks"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for index, token in enumerate(["mine", "peer", "other", "mine"]):
            response = await client.post(path, json={"text": f"goal {index}"},
                                         headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(index)})
            assert response.status_code == 202
        headers = {"Authorization": "Bearer mine"}
        first = await client.get(path, params={"limit": 1}, headers=headers)
        assert first.status_code == 200
        body = first.json()
        assert [row["goal_text"] for row in body["tasks"]] == ["goal 3"]
        assert body["tasks"][0]["created_at"]
        second = await client.get(path, params={"limit": 1, "before": body["next_before"]}, headers=headers)
        assert [row["goal_text"] for row in second.json()["tasks"]] == ["goal 0"]
        assert second.json()["next_before"] is None
        assert body["state_counts"] == second.json()["state_counts"] == {"NEEDS_CLARIFICATION": 2}
        empty = await client.get(path, params={"before": 1}, headers=headers)
        assert empty.json()["tasks"] == []
        assert empty.json()["state_counts"] == body["state_counts"]
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, params={"limit": 0}, headers=headers)).status_code == 422
