"""A dlv/0 test robot: 'serve' command channel, 'send-wav' or paced 'send-pcm' voice upload."""
import argparse
import asyncio
import base64
import io
import json
import logging
import math
import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit
import wave

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

log = logging.getLogger("mock_robot")
DEFAULT_URL = "ws://127.0.0.1:18763/api/voice/robot/ws"


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


def pcm_source(path, silence_ms=1000):
    """Validate the existing WAV format, then extract raw PCM with silence padding."""
    if not isinstance(silence_ms, int) or isinstance(silence_ms, bool) or not 0 <= silence_ms <= 10000:
        raise ValueError("silence_ms must be an integer between 0 and 10000")
    # Keep the same WAV admission rules as send-wav; no ASR or simulated transcript.
    wav_segment(path)
    with wave.open(str(path), "rb") as wav:
        pcm = wav.readframes(wav.getnframes())
    padding = b"\0" * (silence_ms * 32)
    return padding + pcm + padding


def save_pcm_segment(frame, output_dir, index):
    """Save an authenticated server's returned WAV under a locally chosen filename."""
    data = base64.b64decode(frame.get("wav_b64", ""), validate=True)
    with wave.open(io.BytesIO(data), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("platform PCM result must be a 16 kHz mono 16-bit WAV")
        count = wav.getnframes()
        if not count or len(wav.readframes(count)) != count * 2:
            raise ValueError("empty or incomplete platform WAV")
    if frame.get("duration_ms") != count / 16:
        raise ValueError("platform duration does not match returned WAV")
    if frame.get("end_reason") not in {"silence", "max_duration"}:
        raise ValueError("unknown PCM segment end reason")
    segment_id = frame.get("segment_id")
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("platform PCM result has no segment id")
    filename = f"segment-{index:03d}.wav"
    with (output_dir / filename).open("xb") as file:
        file.write(data)
    result = {key: frame.get(key) for key in (
        "segment_id", "duration_ms", "end_reason", "received_chunks", "received_pcm_bytes")}
    result.update(wav_file=filename, wav_bytes=len(data))
    return result


async def send_pcm(url, token, robot_id, path, *, chunk_ms=20, silence_ms=1000,
                   output_dir, timeout=30, expect_task=False):
    """Upload one PCM stream over one WS, stopping on thinking; never reconnect/replay.

    By default only segmentation is required. expect_task also requires a linked
    pcm_task receipt; it is a local assertion, never an authorization flag sent
    to the platform. Task state/transcription in the database remain authoritative.
    A complete upload report means the expected receipts arrived, not that the
    physical task succeeded or that a plan has already been generated.
    """
    if not isinstance(chunk_ms, int) or isinstance(chunk_ms, bool) or not 1 <= chunk_ms <= 1000:
        raise ValueError("chunk_ms must be an integer between 1 and 1000")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if not isinstance(expect_task, bool):
        raise ValueError("expect_task must be a boolean")
    pcm = pcm_source(path, silence_ms)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "pcm-upload-report.json"
    if report_path.exists() or any(output_dir.glob("segment-*.wav")):
        raise ValueError("output directory already contains PCM results; choose a new directory")
    clock = asyncio.get_running_loop().time
    started = clock()
    stop = asyncio.Event()
    chunk_bytes = chunk_ms * 32
    report = {"robot_id": robot_id, "chunk_ms": chunk_ms, "silence_ms": silence_ms,
              "total_pcm_bytes": len(pcm), "sent_chunks": 0, "sent_pcm_bytes": 0,
              "stopped_by_thinking": False, "segments": [], "status": "error",
              "expect_task": expect_task, "tasks": [], "warnings": []}

    async def sender(ws):
        for offset in range(0, len(pcm), chunk_bytes):
            # Absolute audio clock avoids accumulating one sleep's scheduling error per chunk.
            delay = started + offset / 32000 - clock()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                    return
                except TimeoutError:
                    pass
            if stop.is_set():
                return
            chunk = pcm[offset:offset + chunk_bytes]
            await ws.send(json.dumps({"type": "audio", "pcm_b64": base64.b64encode(chunk).decode("ascii")},
                                     separators=(",", ":")))
            report["sent_chunks"] += 1
            report["sent_pcm_bytes"] += len(chunk)

    async def receiver(ws):
        while True:
            frame = json.loads(await ws.recv())
            if not isinstance(frame, dict):
                raise ValueError("invalid PCM response")
            if frame.get("type") == "pcm_segment":
                segment = save_pcm_segment(frame, output_dir, len(report["segments"]) + 1)
                report["segments"].append(segment)
            elif frame.get("type") == "pcm_task":
                fields = ("segment_id", "request_key", "task_id", "state")
                if any(not isinstance(frame.get(key), str) or not frame[key].strip() for key in fields):
                    raise ValueError("incomplete PCM task receipt")
                if frame["segment_id"] not in {segment["segment_id"] for segment in report["segments"]}:
                    raise ValueError("PCM task refers to an unknown segment")
                task = {key: frame[key] for key in fields}
                if "transcript" in frame and frame["transcript"] is not None:
                    if not isinstance(frame["transcript"], str):
                        raise ValueError("invalid PCM task transcript")
                    task["transcript"] = frame["transcript"]
                report["tasks"].append(task)
            elif frame.get("type") == "state":
                state = frame.get("state")
                if state == "thinking":
                    report["stopped_by_thinking"] = True
                    stop.set()
                elif state == "error":
                    reason = str(frame.get("message") or "platform PCM error")
                    if reason == "TTS_UNAVAILABLE":
                        report["warnings"].append(reason)
                        continue
                    raise RuntimeError(reason)
                elif state == "idle":
                    if not report["segments"]:
                        raise RuntimeError("platform returned idle without a completed PCM segment")
                    if expect_task and not report["tasks"]:
                        raise RuntimeError("platform returned idle without a PCM task receipt")
                    return

    try:
        async with asyncio.timeout(timeout), connection(url, token, robot_id) as ws:
            await ready(ws, robot_id)
            started = clock()
            sending = asyncio.create_task(sender(ws))
            receiving = asyncio.create_task(receiver(ws))
            try:
                done, _ = await asyncio.wait({sending, receiving}, return_when=asyncio.FIRST_COMPLETED)
                if sending in done:
                    await sending  # Propagate send failures instead of hanging until timeout.
                await receiving
            finally:
                stop.set()
                for task in (sending, receiving):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sending, receiving, return_exceptions=True)
        report["status"] = "complete"
    except TimeoutError as exc:
        report["error_type"] = "TimeoutError"
        result = "PCM task receipt" if expect_task else "completed PCM segment"
        raise TimeoutError(f"timed out waiting for a {result}") from exc
    except BaseException as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        report["elapsed_s"] = round(clock() - started, 4)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("PCM upload %s: sent_chunks=%d sent_pcm_bytes=%d elapsed_s=%s segments=%d tasks=%d warnings=%s report=%s",
                 report["status"], report["sent_chunks"], report["sent_pcm_bytes"], report["elapsed_s"],
                 len(report["segments"]), len(report["tasks"]), report["warnings"], report_path)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
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
    pcm = sub.add_parser("send-pcm", help="pace raw PCM over one WS; save returned segmentation artifacts")
    pcm.add_argument("--voice-id", default="mock-fridge-01-voice")
    pcm.add_argument("wav", type=Path)
    pcm.add_argument("--chunk-ms", type=int, default=20)
    pcm.add_argument("--silence-ms", type=int, default=1000)
    pcm.add_argument("--timeout", type=float, default=30)
    pcm.add_argument("--output-dir", type=Path, required=True)
    pcm.add_argument("--expect-task", action="store_true", help="also require a pcm_task receipt before idle")
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
        elif args.mode == "send-wav":
            asyncio.run(send_wav(args.url, token, args.voice_id, args.wav))
        else:
            asyncio.run(send_pcm(args.url, token, args.voice_id, args.wav, chunk_ms=args.chunk_ms,
                                 silence_ms=args.silence_ms, output_dir=args.output_dir, timeout=args.timeout,
                                 expect_task=args.expect_task))
    except KeyboardInterrupt:
        pass
    except (OSError, TimeoutError, RuntimeError, ValueError, EOFError, wave.Error, WebSocketException) as exc:
        parser.exit(1, f"simulator: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
