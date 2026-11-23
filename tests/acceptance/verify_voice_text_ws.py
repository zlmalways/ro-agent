"""Opt-in verification against a running real test platform and its configured model.

Uses RO_AGENT_ROBOT_TOKEN. Creates three conversational turns and checks replay.
No platform, model or persistence substitutes are used.
"""
import argparse
import asyncio
import json
import os
from uuid import uuid4

from websockets.asyncio.client import connect


async def verify(url, token):
    async with connect(url, additional_headers={"x-robot-token": token}, proxy=None) as ws:
        assert json.loads(await ws.recv())["state"] == "ready"

        async def turn(frame):
            await ws.send(json.dumps(frame, ensure_ascii=False))
            receipt = None
            async with asyncio.timeout(90):
                while True:
                    event = json.loads(await ws.recv())
                    if event.get("request_key") != frame["request_key"]:
                        continue
                    if event["type"] == "text_task":
                        receipt = event
                    if event["type"] == "state" and event.get("state") == "error":
                        raise RuntimeError(event["message"])
                    if event["type"] == "done":
                        assert receipt is not None
                        assert event["turn_id"] == receipt["turn_id"]
                        assert event["kind"] == "reply" and event["text"]
                        return event
                    if event["type"] == "task_finished":
                        raise RuntimeError(json.dumps(event, ensure_ascii=False))

        def request(text, conversation_id=None):
            frame = {"type": "text", "request_key": "TEST-text-ws-" + uuid4().hex, "text": text}
            if conversation_id:
                frame["conversation_id"] = conversation_id
            return frame

        first_request = request("你好，我叫小红。")
        first = await turn(first_request)
        second_request = request("我叫什么名字？", first["task_id"])
        second = await turn(second_request)
        third = await turn(request("谢谢。", first["task_id"]))
        assert first["task_id"] == second["task_id"] == third["task_id"]
        assert len({first["turn_id"], second["turn_id"], third["turn_id"]}) == 3
        assert "小红" in second["text"]
        replay = await turn(second_request)
        assert replay["turn_id"] == second["turn_id"]
        assert replay["text"] == second["text"]

        # Reusing a request key for different content must fail without creating a turn.
        await ws.send(json.dumps({**second_request, "text": "改变后的内容"}, ensure_ascii=False))
        async with asyncio.timeout(10):
            while True:
                event = json.loads(await ws.recv())
                if event.get("request_key") == second_request["request_key"]:
                    assert event["type"] == "state" and event["state"] == "error"
                    break
        return {"test": "voice-text-ws", "conversation_id": first["task_id"],
                "turns": [first, second, third], "replay_original_turn": True,
                "conflicting_replay_rejected": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.url, os.environ["RO_AGENT_ROBOT_TOKEN"])),
                     ensure_ascii=False, indent=2))
