"""A dlv/0 test robot. Start the command client with 'serve', upload one turn with 'send-wav'."""
import argparse
import asyncio
import base64
import io
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit
import wave

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

log = logging.getLogger("mock_robot")


def connection(url, token, robot_id):
    parts = urlsplit(url)
    if parts.scheme not in {"ws", "wss"} or not parts.hostname or parts.query or parts.fragment:
        raise ValueError("expected a ws(s) endpoint without query or fragment")
    address = urlunsplit(parts._replace(query=urlencode({"robot_id": robot_id})))
    return connect(address, additional_headers={"x-robot-token": token}, proxy=None, open_timeout=8)


async def ready(ws, robot_id, *, command=False):
    async with asyncio.timeout(8):
        frame = json.loads(await ws.recv())
    field, value = ("channel", "command") if command else ("protocol", "dlv/0")
    if not (frame.get("type") == "state" and frame.get("state") == "ready"
            and frame.get("robot_id") == robot_id and frame.get(field) == value):
        raise ValueError("unexpected robot handshake")


def status(command_id, state, detail):
    return {"type": "cmd_status", "cmd_id": command_id, "state": state,
            "progress": 0.0 if state == "running" else 1.0, "detail": detail}


class Simulator:
    def __init__(self, robot_id, map_name, waypoints, skill_ids, duration):
        self.robot_id = robot_id
        self.map_name = map_name
        self.waypoints = waypoints
        self.skill_ids = skill_ids
        self.duration = duration
        self.active = None
        self.finish_at = 0.0
        self.replies = {}

    def hello(self):
        skills = ["move_to", "vla_skill"] if self.skill_ids else ["move_to"]
        return {"type": "hello", "robot_id": self.robot_id, "executor": "simulator",
                "busy_cmd_id": self.active or "", "skills": skills}

    def readiness(self):
        return {"type": "readiness", "executor": "simulator", "busy": self.active is not None,
                "map": self.map_name, "waypoints": len(self.waypoints), "waypoint_names": self.waypoints,
                "nav_server_ready": True, "skill_server_ready": bool(self.skill_ids),
                "brain_server_ready": bool(self.skill_ids), "brain_engine": "ready" if self.skill_ids else "off",
                "brain_skills": [{"skill_id": key} for key in self.skill_ids]}

    def finish(self, state, detail):
        reply = status(self.active, state, detail)
        self.replies[self.active] = reply
        self.active = None
        return reply

    def handle(self, frame, now):
        key = frame.get("cmd_id")
        if not isinstance(key, str) or not key:
            return None
        if frame.get("type") == "cancel":
            if self.active == key:
                return self.finish("failed", "cancelled by platform")
            return self.replies.get(key)
        if frame.get("type") != "command":
            return None
        if key in self.replies:
            return self.replies[key]
        args = frame.get("args")
        skill = frame.get("skill")
        if self.active is not None:
            reply = status(key, "failed", "simulator busy")
        elif not isinstance(args, dict) or not (
            (skill == "move_to" and args.get("location") in self.waypoints)
            or (skill == "vla_skill" and args.get("skill_id") in self.skill_ids)
        ):
            reply = status(key, "failed", "unknown action, waypoint or skill")
        else:
            self.active = key
            self.finish_at = now + self.duration
            reply = status(key, "running", f"simulating {skill}")
        self.replies[key] = reply
        return reply


async def command_session(url, token, robot, interval=10):
    clock = asyncio.get_running_loop().time
    async with connection(url, token, robot.robot_id) as ws:
        await ready(ws, robot.robot_id)
        await ws.send(json.dumps(robot.hello()))
        await ready(ws, robot.robot_id, command=True)
        log.info("command connected: %s map=%s waypoints=%s", robot.robot_id, robot.map_name, robot.waypoints)
        next_report = clock()
        try:
            while True:
                now = clock()
                if robot.active is not None and now >= robot.finish_at:
                    result = robot.finish("succeeded", "simulated action complete")
                    await ws.send(json.dumps(result))
                    log.info("%s %s", result["cmd_id"], result["state"])
                    next_report = now
                if now >= next_report:
                    await ws.send(json.dumps(robot.readiness()))
                    next_report = now + interval
                deadline = min(next_report, robot.finish_at) if robot.active is not None else next_report
                try:
                    raw = await asyncio.wait_for(ws.recv(), max(0.001, deadline - clock()))
                except TimeoutError:
                    continue
                frame = json.loads(raw)
                if frame.get("type") in {"command", "cancel"}:
                    log.info("received %s", json.dumps(frame, ensure_ascii=False))
                reply = robot.handle(frame, clock())
                if reply:
                    await ws.send(json.dumps(reply))
                    log.info("%s %s %s", reply["cmd_id"], reply["state"], reply["detail"])
                    next_report = clock()
        except ConnectionClosed as exc:
            if exc.rcvd is None or exc.rcvd.code not in (1000, 1001):
                raise
        finally:
            if robot.active is not None:
                robot.finish("failed", "simulated action stopped on disconnect")


async def serve_robot(url, token, robot):
    while True:
        try:
            await command_session(url, token, robot)
        except (OSError, TimeoutError, WebSocketException) as exc:
            log.warning("command connection unavailable: %s", exc)
        await asyncio.sleep(3)


def wav_segment(path):
    data = path.read_bytes()
    with wave.open(io.BytesIO(data), "rb") as wav:
        count = wav.getnframes()
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("WAV must contain 16 kHz mono 16-bit PCM")
        if not count or len(wav.readframes(count)) != count * 2:
            raise ValueError("empty or incomplete WAV")
    return {"type": "segment", "woke": True, "wav_b64": base64.b64encode(data).decode("ascii")}


async def send_wav(url, token, robot_id, path, timeout=90):
    payload = json.dumps(wav_segment(path), separators=(",", ":"))
    async with asyncio.timeout(timeout), connection(url, token, robot_id) as ws:
        await ready(ws, robot_id)
        await ws.send(payload)
        log.info("sent one WAV (%d bytes on wire) to %s", len(payload), robot_id)
        errors = []
        while True:
            frame = json.loads(await ws.recv())
            if frame.get("type") == "audio":
                continue
            log.info("voice reply %s", {k: frame[k] for k in ("type", "state", "message", "text", "transcript") if k in frame})
            if frame.get("type") == "state" and frame.get("state") == "error":
                reason = str(frame.get("message", "voice error"))
                if reason != "TTS_UNAVAILABLE":
                    errors.append(reason)
            if frame.get("type") == "state" and frame.get("state") == "idle":
                if errors:
                    raise RuntimeError(", ".join(errors))
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="platform WebSocket endpoint (ws:// or wss://)")
    sub = parser.add_subparsers(dest="mode", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--robot-id", default="mock-fridge-01")
    serve.add_argument("--map", default="模拟办公区")
    serve.add_argument("--waypoint", action="append")
    serve.add_argument("--skill-id", action="append", default=[])
    serve.add_argument("--duration", type=float, default=1.0)
    upload = sub.add_parser("send-wav")
    upload.add_argument("--voice-id", default="mock-fridge-01-voice")
    upload.add_argument("wav", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("RO_AGENT_ROBOT_TOKEN")
    if not token:
        parser.error("set RO_AGENT_ROBOT_TOKEN for the simulator binding")
    try:
        if args.mode == "serve":
            if not 0 < args.duration <= 3600:
                parser.error("--duration must be between 0 and 3600 seconds")
            robot = Simulator(args.robot_id, args.map, args.waypoint or ["前台", "充电点"], args.skill_id, args.duration)
            asyncio.run(serve_robot(args.url, token, robot))
        else:
            asyncio.run(send_wav(args.url, token, args.voice_id, args.wav))
    except KeyboardInterrupt:
        pass
    except (OSError, TimeoutError, RuntimeError, ValueError, EOFError, wave.Error, WebSocketException) as exc:
        parser.exit(1, f"simulator: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
