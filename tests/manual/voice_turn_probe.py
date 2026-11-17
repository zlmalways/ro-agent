"""TEST ONLY: replay PCM into a real platform and observe correlated turn outcomes.

No ASR, planner, task service or database implementation is substituted. The
caller owns the WebSocket so successive captures can share the same connection.
"""
import asyncio
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from tests.manual.mock_robot_ws import (
    DEFAULT_MAP, DEFAULT_VLA_SKILL_IDS, DEFAULT_WAYPOINTS, Simulator,
    connection, pcm_source, ready, save_pcm_segment,
)


class VoiceTurnProbe:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.reports = []
        self.events = self.output_dir / "TEST-voice-events.jsonl"
        self.events.write_text(json.dumps({"test_only": True, "event": "start"}) + "\n", encoding="utf-8")

    def record(self, event, **fields):
        entry = {"test_only": True, "at": datetime.now(timezone.utc).isoformat(),
                 "monotonic": time.monotonic(), "event": event, **fields}
        with self.events.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def turn(self, ws, wav_path, *, timeout=180, inject_tail=False):
        capture_id = str(uuid4())
        pcm = pcm_source(Path(wav_path), silence_ms=1200)
        report = {"test_only": True, "capture_id": capture_id, "input": str(wav_path),
                  "sent_chunks": 0, "sent_bytes": 0, "frames": [], "status": "running"}
        self.reports.append(report)
        stop = asyncio.Event()
        clock = asyncio.get_running_loop().time
        start = clock()
        self.record("capture_started", capture_id=capture_id, input=str(wav_path))

        async def send():
            for offset in range(0, len(pcm), 640):
                delay = start + offset / 32000 - clock()
                if delay > 0:
                    try:
                        await asyncio.wait_for(stop.wait(), delay)
                        return
                    except TimeoutError:
                        pass
                if stop.is_set():
                    return
                chunk = pcm[offset:offset + 640]
                await ws.send(json.dumps({"type": "audio", "capture_id": capture_id,
                                          "pcm_b64": base64.b64encode(chunk).decode("ascii")}))
                report["sent_chunks"] += 1
                report["sent_bytes"] += len(chunk)

        sending = asyncio.create_task(send())
        segment_id = task_id = turn_id = None
        version = -1
        try:
            async with asyncio.timeout(timeout):
                while True:
                    if sending.done():
                        await sending
                    frame = json.loads(await ws.recv())
                    if not isinstance(frame, dict):
                        raise AssertionError("Non-object server frame")
                    visible = {key: value for key, value in frame.items()
                               if key not in {"wav_b64", "pcm_b64", "audio_b64"}}
                    self.record("received", frame=visible)
                    kind = frame.get("type")
                    if kind == "idle" or (kind == "state" and frame.get("state") == "idle"):
                        raise AssertionError("Removed voice idle was sent")
                    if kind == "pcm_activity":
                        raise AssertionError("Removed pcm_activity was sent")
                    if kind == "state" and frame.get("state") == "error":
                        raise AssertionError("Platform error: " + str(frame.get("message")))
                    if kind not in {"pcm_segment", "pcm_task", "task_state", "done", "task_finished"}:
                        continue
                    if frame.get("capture_id") != capture_id:
                        raise AssertionError("Wrong capture_id in voice response")
                    report["frames"].append(visible)
                    if kind == "pcm_segment":
                        if segment_id is not None:
                            raise AssertionError("More than one segment accepted for this capture")
                        stop.set()
                        await sending
                        segment = save_pcm_segment(frame, self.output_dir, len(self.reports))
                        segment_id = segment["segment_id"]
                        report["segment"] = segment
                        self.record("upload_stopped", capture_id=capture_id,
                                    sent_chunks=report["sent_chunks"], sent_bytes=report["sent_bytes"])
                        if inject_tail:
                            # Re-send the same speech under the consumed capture ID.
                            # It must be discarded before ASR/task admission.
                            for offset in range(0, len(pcm), 640):
                                await ws.send(json.dumps({"type": "audio", "capture_id": capture_id,
                                    "pcm_b64": base64.b64encode(pcm[offset:offset + 640]).decode("ascii")}))
                            self.record("consumed_capture_replayed", capture_id=capture_id, bytes=len(pcm))
                        continue
                    if not segment_id or frame.get("segment_id") != segment_id:
                        raise AssertionError("Task notification is not linked to the received WAV")
                    incoming_version = frame.get("version")
                    if type(incoming_version) is not int or incoming_version < version:
                        raise AssertionError("Invalid or decreasing task version")
                    version = incoming_version
                    if kind == "pcm_task":
                        if task_id is not None:
                            raise AssertionError("Duplicate task admission")
                        task_id, turn_id = frame.get("task_id"), frame.get("turn_id")
                        if not task_id or not turn_id:
                            raise AssertionError("Task admission missing identifiers")
                        report.update(task_id=task_id, turn_id=turn_id)
                        continue
                    if not task_id or (frame.get("task_id"), frame.get("turn_id")) != (task_id, turn_id):
                        raise AssertionError("Notification arrived before admission or for another turn")
                    if kind == "task_state":
                        if frame.get("turn_finished") is not False:
                            raise AssertionError("A progress notification claims the turn finished")
                        continue
                    if frame.get("turn_finished") is not True:
                        raise AssertionError("End notification lacks turn_finished")
                    if kind == "done" and (frame.get("state") != "REPLIED"
                            or frame.get("playback") != "none" or frame.get("play_secs") != 0):
                        raise AssertionError("Unexpected text-only completion")
                    report.update(status="complete", end=visible)
                    return report
        except BaseException as exc:
            report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            stop.set()
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
            report["elapsed_s"] = round(clock() - start, 3)
            (self.output_dir / "TEST-voice-report.json").write_text(
                json.dumps(self.reports, ensure_ascii=False, indent=2), encoding="utf-8")


class ControlledTestRobot:
    """TEST ONLY: select robot protocol outcomes, never modify platform state."""
    def __init__(self, url, token, record, *, duration=1.0):
        self.url, self.token, self.record = url, token, record
        self.mode = 'success'
        self.duration = duration
        self.connected = asyncio.Event()
        self.reconnected = asyncio.Event()
        self.commands = []
        self.ws = None

    async def run(self):
        reconnect = False
        while True:
            robot = Simulator('mock-fridge-01', DEFAULT_MAP, list(DEFAULT_WAYPOINTS),
                              list(DEFAULT_VLA_SKILL_IDS), 1.0)
            async with connection(self.url, self.token, robot.robot_id) as ws:
                self.ws = ws
                await ready(ws, robot.robot_id)
                await ws.send(json.dumps(robot.hello()))
                await ready(ws, robot.robot_id, command=True)
                await ws.send(json.dumps(robot.readiness()))
                self.connected.set()
                if reconnect:
                    self.reconnected.set()
                self.record('TEST_command_connected', reconnected=reconnect)
                last_report = time.monotonic()
                while True:
                    now = time.monotonic()
                    if robot.active and now >= robot.finish_at:
                        outcome = 'failed' if self.mode == 'fail' else 'succeeded'
                        result = robot.finish(outcome, 'TEST ONLY simulated action ' + outcome)
                        await ws.send(json.dumps(result))
                        self.record('TEST_robot_report', frame=result)
                    if now - last_report >= 0.5:
                        await ws.send(json.dumps(robot.readiness()))
                        last_report = now
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 0.1)
                    except TimeoutError:
                        continue
                    frame = json.loads(raw)
                    if frame.get('type') == 'command':
                        self.commands.append(frame)
                        self.record('TEST_command_received', frame=frame)
                    robot.duration = self.duration
                    reply = robot.handle(frame, now)
                    if reply:
                        await ws.send(json.dumps(reply))
                        self.record('TEST_robot_report', frame=reply)
                    if frame.get('type') == 'command' and reply and reply['state'] == 'running' and self.mode == 'disconnect':
                        self.mode = 'success'
                        self.connected.clear()
                        self.record('TEST_disconnect_during_action', cmd_id=frame['cmd_id'])
                        await ws.close(code=1000, reason='TEST simulated command link loss')
                        reconnect = True
                        break
            await asyncio.sleep(1.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--voice-id', default='mock-fridge-01-voice')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--inject-consumed-tail', action='store_true')
    parser.add_argument('wav', type=Path, nargs='+')
    args = parser.parse_args()
    token = os.environ.get('RO_AGENT_ROBOT_TOKEN')
    if not token:
        parser.error('set RO_AGENT_ROBOT_TOKEN for the test robot binding')
    if not 0 < args.timeout <= 3600:
        parser.error('--timeout must be between 0 and 3600 seconds')

    async def run():
        probe = VoiceTurnProbe(args.output_dir)
        async with connection(args.url, token, args.voice_id) as ws:
            await ready(ws, args.voice_id)
            for wav in args.wav:
                report = await probe.turn(ws, wav, timeout=args.timeout,
                                          inject_tail=args.inject_consumed_tail)
                print(json.dumps({'test_only': True, 'task_id': report['task_id'],
                                  'end_type': report['end']['type'],
                                  'state': report['end']['state']}, ensure_ascii=False))
    asyncio.run(run())


if __name__ == '__main__':
    main()
