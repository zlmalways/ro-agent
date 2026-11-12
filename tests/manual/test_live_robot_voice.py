"""Opt-in, one-upload acceptance with the configured providers and the local simulator."""
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest

from tests.manual.mock_robot_ws import send_wav


def live_voice_enabled(environment):
    return environment.get("RO_AGENT_RUN_LIVE_VOICE") == "1"


def configured_test_ws_url(environment):
    """Require an explicit loopback target; never inherit the old platform default."""
    value = environment.get("RO_AGENT_TEST_WS_URL", "")
    try:
        target = urlsplit(value)
        valid = (target.scheme in {"ws", "wss"}
                 and target.hostname in {"127.0.0.1", "localhost", "::1"}
                 and target.path == "/api/voice/robot/ws"
                 and not (target.username or target.password or target.query or target.fragment)
                 and target.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("set RO_AGENT_TEST_WS_URL to an explicit loopback /api/voice/robot/ws endpoint")
    return value


@pytest.mark.acceptance
@pytest.mark.skipif(not live_voice_enabled(os.environ), reason="requires the running local voice test deployment")
async def test_spoken_front_desk_reaches_robot_and_finishes():
    ws_url = configured_test_ws_url(os.environ)
    dsn = os.environ["RO_AGENT_TEST_DSN"]
    target = urlsplit(dsn)
    assert target.hostname == "127.0.0.1" and target.path == "/ro_agent_sim_test" and not target.query
    configuration = json.loads(Path(os.environ["RO_AGENT_CONFIG_FILE"]).read_text(encoding="utf-8-sig"))
    asr_settings = configuration["voice_asr"]
    provider = asr_settings["provider"]
    binding = "mock-fridge-01"
    customer = "local-simulator-test"
    wav = Path(__file__).parent / "fixtures" / "front-desk.wav"
    digest = sha256(b"audio/wav\x00" + wav.read_bytes()).hexdigest()
    assert provider in {"openai", "doubao"}
    prefix = f"openai:{asr_settings['model']}:req_" if provider == "openai" else "doubao:"
    db = await asyncpg.connect(dsn)
    try:
        started = await db.fetchval("SELECT clock_timestamp()")
        print(f"upload_started={started.isoformat()} voice={binding}-voice digest={digest}", flush=True)
        await send_wav(ws_url, os.environ["RO_AGENT_ROBOT_TOKEN"], binding + "-voice", wav, timeout=300)
        matches = await db.fetch("""SELECT input_id, state, normalized_text, provider_ref, result_task_id FROM ra_inputs
            WHERE created_at >= $1 AND request_digest=$2 AND customer_id=$3 AND actor_id=$4""",
            started, digest, customer, binding)
        assert len(matches) == 1, "expected one new input for this robot and WAV"
        row = matches[0]
        print("input", dict(row), flush=True)
        assert row["state"] == "COMPLETE" and row["result_task_id"]
        assert row["normalized_text"].strip().rstrip("。.!！") == "请去前台"
        assert row["provider_ref"].startswith(prefix) and len(row["provider_ref"]) > len(prefix)
        task_id = row["result_task_id"]
        async with asyncio.timeout(60):
            while True:
                task = await db.fetchrow("""SELECT state,reason_code,active_plan_id,actor_id,actor_kind,goal_text
                    FROM ra_tasks WHERE task_id=$1 AND customer_id=$2""", task_id, customer)
                if task["state"] == "SUCCEEDED":
                    break
                assert task["state"] in {"RECEIVED", "PLANNING", "READY", "QUEUED", "RUNNING"}, dict(task)
                await asyncio.sleep(0.2)
        assert task["actor_kind"] == "robot" and task["actor_id"] == binding
        assert task["goal_text"] == row["normalized_text"]
        plan_id = task["active_plan_id"]
        steps = json.loads(await db.fetchval("""SELECT steps FROM ra_plans
            WHERE customer_id=$1 AND task_id=$2 AND plan_id=$3""", customer, task_id, plan_id))
        assert len(steps) == 1 and steps[0]["action"] == "move_to"
        assert steps[0]["args"] == {"location": "前台"} and steps[0]["depends_on"] == []
        commands = await db.fetch("""SELECT command_id, plan_id, step_id, binding_id, dispatch_generation,
            dispatch_state, wire_skill, wire_args, sent_at, outcome, evidence_conflict
            FROM ra_commands WHERE customer_id=$1 AND task_id=$2""", customer, task_id)
        assert len(commands) == 1
        cmd = commands[0]
        assert (cmd["plan_id"], cmd["step_id"], cmd["binding_id"]) == (plan_id, steps[0]["step_id"], binding)
        assert cmd["wire_skill"] == steps[0]["action"] and json.loads(cmd["wire_args"]) == steps[0]["args"]
        assert cmd["dispatch_state"] == "SENT" and cmd["sent_at"] and cmd["outcome"] == "SUCCEEDED"
        assert not cmd["evidence_conflict"]
        events = await db.fetch("""SELECT event_id, received_at, payload FROM ra_robot_events
            WHERE customer_id=$1 AND cmd_id=$2 AND binding_id=$3 AND connection_generation=$4 AND event_type='cmd_status'
            ORDER BY received_at,event_id""", customer, cmd["command_id"], binding, cmd["dispatch_generation"])
        receipts = [{**json.loads(e["payload"]), "at": e["received_at"].isoformat(), "event_id": e["event_id"]}
                    for e in events]
        assert [e["state"] for e in receipts] == ["running", "succeeded"], receipts
        print(json.dumps({"task_id": task_id, "plan_id": plan_id, "steps": steps, "command": dict(cmd),
                          "receipts": receipts, "task_state": task["state"]}, ensure_ascii=False, default=str))
    finally:
        await db.close()
