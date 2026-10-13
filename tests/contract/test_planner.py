import json

import httpx
import pytest

from robot_agent.adapters.llm import OpenAICompatiblePlanner
from robot_agent.config import ProviderConfig, load_provider_config
from robot_agent.domain.models import PlanError, TrustedContext
from tests.fixtures.minimal import TestResources as ResourcesFixture


@pytest.mark.parametrize("reserved", ["source_refs", "customer_id", "approval"])
async def test_planner_rejects_reserved_fields(reserved):
    candidate = {"outcome": "plan", "steps": [{"step_id": "s1", "action": "move_to", "args": {"location": "test-table"}, "depends_on": []}], reserved: "forged"}
    async def response(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(candidate)}}]})
    config = ProviderConfig("test", "https://test-provider/v1", "test-secret", "test-model", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        planner = OpenAICompatiblePlanner(config, client=client, max_response_bytes=4096)
        snapshot = await ResourcesFixture().snapshot(TrustedContext("test-customer", "user", "test-user"))
        with pytest.raises(PlanError):
            await planner.plan("test", [], snapshot)


async def test_planner_receives_semantics_without_trusted_identity_or_refs():
    from dataclasses import replace
    observed = []
    def response(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"outcome":"clarify","question":"which?","required_slots":["location"]}'}}]})
    config = ProviderConfig("test", "https://test-provider/v1", "test-secret", "test-model", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        planner = OpenAICompatiblePlanner(config, client=client, max_response_bytes=4096)
        snapshot = await ResourcesFixture().snapshot(TrustedContext("test-customer", "user", "test-user"))
        snapshot = replace(snapshot, capability_labels={"test-pick": "pick up a cup"}, environment_labels={"test-table": "table by the entrance"})
        result = await planner.plan("test goal", [], snapshot)
        assert result.outcome == "clarify"
        payload = json.loads(observed[0]["messages"][-1]["content"])
        assert "customer_id" not in payload
        assert "source_refs" not in payload
        assert "test-table" in payload["waypoints"]
        assert payload["capability_labels"] == {"test-pick": "pick up a cup"}


def test_legacy_provider_is_selected_only_by_explicit_config():
    assert load_provider_config({}) is None
    config = load_provider_config({"DEEPLEAP_LLM_PROVIDER": "deepseek", "DEEPSEEK_BASE_URL": "https://test/v1",
                                   "DEEPSEEK_MODEL": "test-model", "DEEPSEEK_API_KEY": "test-only"})
    assert config.name == "deepseek"
    assert config.model == "test-model"


async def test_planner_timeout_is_dependency_failure():
    from robot_agent.domain.models import DependencyUnavailable
    def response(request):
        raise httpx.ReadTimeout("test", request=request)
    config = ProviderConfig("test", "https://test-provider/v1", "test-secret", "test-model", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        planner = OpenAICompatiblePlanner(config, client=client, max_response_bytes=4096)
        snapshot = await ResourcesFixture().snapshot(TrustedContext("test-customer", "user", "test-user"))
        with pytest.raises(DependencyUnavailable):
            await planner.plan("test", [], snapshot)
