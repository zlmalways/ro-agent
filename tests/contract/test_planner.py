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


@pytest.mark.parametrize("provider", ["gpt", "deepseek"])
async def test_existing_providers_keep_chat_completions_contract(provider):
    prefix = provider.upper()
    config = load_provider_config({
        "DEEPLEAP_LLM_PROVIDER": provider,
        f"{prefix}_BASE_URL": "https://provider.test/v1/",
        f"{prefix}_API_KEY": "test-only",
        f"{prefix}_MODEL": "test-model",
    })
    assert config.name == provider and config.timeout_seconds == 30
    candidate = {"outcome": "plan", "steps": [{"step_id": "s1", "action": "move_to",
                 "args": {"location": "test-table"}, "depends_on": []}]}

    def response(request):
        assert str(request.url) == "https://provider.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only"
        body = json.loads(request.content)
        assert body["model"] == "test-model" and body["temperature"] == 0
        assert body["response_format"] == {"type": "json_object"}
        assert [message["role"] for message in body["messages"]] == ["system", "user"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(candidate)}}]})

    snapshot = await ResourcesFixture().snapshot(TrustedContext("test-customer", "user", "test-user"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await OpenAICompatiblePlanner(config, client=client).plan("test", [], snapshot)
    assert result.outcome == "plan" and result.steps[0].args == {"location": "test-table"}


def _relay_config():
    return load_provider_config({
        "DEEPLEAP_LLM_PROVIDER": "relay",
        "RELAY_BASE_URL": "http://relay.test/v1/",
        "RELAY_MODEL": "test-relay-model",
        "RELAY_API_KEY": "test-only",
    })


def _message(*parts):
    return {"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": part} for part in parts]}


@pytest.mark.parametrize("candidate", [
    {"outcome": "plan", "steps": [{"step_id": "s1", "action": "move_to",
                                    "args": {"location": "test-table"}, "depends_on": []}]},
    {"outcome": "clarify", "question": "Where?", "required_slots": ["location"]},
    {"outcome": "unsupported", "reason": "No matching skill"},
])
async def test_relay_uses_responses_and_parses_candidate(candidate):
    config = _relay_config()
    assert config.timeout_seconds == 240
    snapshot = await ResourcesFixture().snapshot(TrustedContext("test-customer", "user", "test-user"))
    def response(request):
        assert str(request.url) == "http://relay.test/v1/responses"
        assert request.headers["authorization"] == "Bearer test-only"
        body = json.loads(request.content)
        assert body["model"] == "test-relay-model" and body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["type"] == "json_schema"
        content = body["input"][-1]["content"]
        assert [part["type"] for part in content] == ["input_text"]
        context = json.loads(content[0]["text"])
        assert context["answers"] == ["my answer"] and "test-table" in context["waypoints"]
        assert "customer_id" not in context and "source_refs" not in context
        formatted = {"steps": [], "reason": "", "question": "", "required_slots": [], **candidate}
        content = json.dumps(formatted)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "reasoning"}, _message(content[:8], content[8:])]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await OpenAICompatiblePlanner(config, client=client).plan("test", ["my answer"], snapshot)
    assert result.outcome == candidate["outcome"]
    if result.steps:
        assert result.steps[0].args == {"location": "test-table"}


@pytest.mark.parametrize("body", [
    {"status": "incomplete", "output": [_message('{"outcome":"unsupported"}')]},
    {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": '{"outcome":"unsupported"}'}, {"type": "refusal"}]}]},
    {"status": "completed", "output": [_message('{"outcome":"unsupported","approval":true}')]},
    {"status": "completed", "output": []},
])
async def test_relay_rejects_unusable_output(body):
    snapshot = await ResourcesFixture().snapshot(TrustedContext("test-customer", "user", "test-user"))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PlanError, match="PLANNER_INVALID_OUTPUT"):
            await OpenAICompatiblePlanner(_relay_config(), client=client).plan("test", [], snapshot)
