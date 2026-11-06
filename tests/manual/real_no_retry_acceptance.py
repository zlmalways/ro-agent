"""Real HTTP/WS acceptance against an explicitly isolated release instance.

Only the robot protocol peer is simulated. This script imports no application
business code and never supplies planner output or writes the database.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


API = "/api/robot-agent"
WAYPOINTS = ["验收A点（协议模拟）", "验收B点（协议模拟）"]
SKILL = "test_no_retry_pick"
GOAL = f"请先移动到{WAYPOINTS[0]}，到达后再移动到{WAYPOINTS[1]}。两次移动必须按顺序完成。"
STABLE = {"WAITING_APPROVAL", "NEEDS_CLARIFICATION", "BLOCKED", "FAILED", "CANCELLED", "SUCCEEDED"}
CASES = (
    "success", "failed", "timeout", "late_success", "duplicate", "cancel",
    "no_robot", "state_unknown", "not_ready", "queue", "concurrent_reads", "clarification",
    "waypoint_removed", "skill_removed", "waypoint_expired", "skill_expired",
    "missing_waypoint", "missing_skill", "permission_denied", "model_failure",
    "disconnect_unknown", "send_timeout", "conflict", "untracked", "restart_prepare", "restart_verify",
    "ui_approval_hold",
)


def utc():
    return datetime.now(timezone.utc).isoformat()


class NotVerified(RuntimeError):
    """The real prerequisite was not obtained; do not fabricate it."""


class IsolatedWebSocket(connect):
    """Never follow a handshake redirect outside the checked endpoint."""
    def process_redirect(self, exc):
        return exc


def safe_base_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port not in {18873, 18874}
            or parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("only http://127.0.0.1:18873 or :18874 is permitted; production/robot addresses are forbidden")
    return f"http://127.0.0.1:{parts.port}"


def load_credentials(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    for key in ("web_token", "robot_token", "robot_id", "binding_id"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"credentials file requires {key}")
    for key in ("robot_id", "binding_id"):
        if not value[key].startswith("test-no-retry-"):
            raise ValueError(f"{key} must start with test-no-retry-; real robot identities are forbidden")
    return value


class Evidence:
    def __init__(self, directory, secrets=()):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.secrets = [value for value in secrets if isinstance(value, str) and value]
        self.current = "setup"
        self.requests = Counter()
        self.records = []

    def clean(self, value):
        raw = json.dumps(value, ensure_ascii=False, default=str)
        for secret in sorted(self.secrets, key=len, reverse=True):
            raw = raw.replace(secret, "[REDACTED]")
        return json.loads(raw)

    def record(self, kind, **payload):
        row = self.clean({"at": utc(), "case": self.current, "kind": kind, **payload})
        self.records.append(row)
        with (self.directory / f"{self.current}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def save(self, name, value):
        (self.directory / name).write_text(json.dumps(self.clean(value), ensure_ascii=False, indent=2), encoding="utf-8")


class RobotPeer:
    """A labelled dlv/0 test peer; commands are observed, never physically executed."""
    def __init__(self, suite, mode="ready"):
        self.suite, self.mode = suite, mode
        self.ws = None
        self.jobs = []
        self.received = []
        self.queue = asyncio.Queue()
        self.active = None
        self.reporting = True
        self.waypoints = list(WAYPOINTS)
        self.skills = [SKILL]
        self.expire_resources = False

    async def __aenter__(self):
        url = self.suite.base.replace("http://", "ws://") + "/api/voice/robot/ws?" + urlencode(
            {"robot_id": self.suite.creds["robot_id"]})
        self.ws = await IsolatedWebSocket(url, additional_headers={"x-robot-token": self.suite.creds["robot_token"]},
                                         proxy=None, open_timeout=10, close_timeout=3)
        await self.handshake("protocol", "dlv/0")
        await self.send({"type": "hello", "robot_id": self.suite.creds["robot_id"],
                         "executor": "no-retry-acceptance-protocol-simulator", "skills": ["move_to", "vla_skill"]})
        await self.handshake("channel", "command")
        if self.mode != "unknown":
            await self.report()
        await self.send({"type": "telemetry", "battery": 98, "location": "隔离验收模拟区域", "observed_at": utc()})
        self.jobs = [asyncio.create_task(self.reader()), asyncio.create_task(self.reporter())]
        await self.suite.eventually(lambda: self.suite.connected(self.mode), timeout=10,
                                    description="test robot connection/readiness")
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    async def disconnect(self):
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        self.jobs = []
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def handshake(self, key, value):
        frame = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
        assert frame.get("type") == "state" and frame.get("state") == "ready", frame
        assert frame.get(key) == value and frame.get("robot_id") == self.suite.creds["robot_id"], frame
        self.suite.log.record("ws_handshake", frame=frame)

    async def send(self, frame):
        self.suite.log.record("ws_send", frame=frame, simulated_robot=True)
        await self.ws.send(json.dumps(frame, ensure_ascii=False))

    async def report(self):
        frame = {"type": "readiness", "executor": "no-retry-acceptance-protocol-simulator",
                 "busy": self.active is not None, "nav_server_ready": self.mode != "not_ready",
                 "skill_server_ready": True, "brain_server_ready": True, "brain_engine": "ready"}
        if not self.expire_resources:
            frame.update({"map": "no-retry-acceptance-map", "waypoints": len(self.waypoints),
                          "waypoint_names": self.waypoints, "preload": "loaded",
                          "brain_skills": [{"skill_id": key} for key in self.skills]})
        await self.send(frame)

    async def reporter(self):
        while True:
            await asyncio.sleep(1)
            if self.reporting and self.mode != "unknown":
                await self.report()

    async def reader(self):
        try:
            async for raw in self.ws:
                frame = json.loads(raw)
                self.suite.log.record("ws_receive", frame=frame)
                if frame.get("type") in {"command", "cancel"}:
                    self.received.append(frame)
                    await self.queue.put(frame)
        except ConnectionClosed as exc:
            self.suite.log.record("ws_closed", code=exc.code)

    @property
    def commands(self):
        return [frame for frame in self.received if frame["type"] == "command"]

    async def next_command(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = await asyncio.wait_for(self.queue.get(), max(.01, deadline - time.monotonic()))
            if frame["type"] == "command":
                args = frame.get("args", {})
                assert ((frame.get("skill") == "move_to" and args.get("location") in WAYPOINTS)
                        or (frame.get("skill") == "vla_skill" and args.get("skill_id") == SKILL)), frame
                self.active = frame["cmd_id"]
                return frame
        raise TimeoutError("no command received")

    async def status(self, command, state, detail=""):
        if state in {"succeeded", "failed"}:
            self.active = None
            if self.reporting:
                await self.report()
        await self.send({"type": "cmd_status", "cmd_id": command["cmd_id"], "state": state,
                         "detail": detail or f"protocol simulator reports {state}",
                         **({"error_code": "TEST_EXECUTOR_FAILED"} if state == "failed" else {})})


class Suite:
    def __init__(self, args):
        self.args = args
        self.base = safe_base_url(args.base_url)
        self.creds = load_credentials(args.credentials_path)
        run = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        self.log = Evidence(Path(args.evidence_dir) / run,
                            [value for key, value in self.creds.items() if "token" in key or "password" in key])
        self.client = httpx.AsyncClient(base_url=self.base, timeout=20, trust_env=False, follow_redirects=False)
        self.tasks = {}
        self.results = []
        self.task_meta = {}
        self.task_event_counts = {}

    async def request(self, method, path, *, body=None, key=None, token=None, expected=(200,)):
        headers = {"Authorization": "Bearer " + (token if token is not None else self.creds["web_token"])}
        if key:
            headers["Idempotency-Key"] = key
        self.log.requests[f"{method} {path.split('?')[0]}"] += 1
        started = time.monotonic()
        try:
            response = await self.client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            self.log.record("http_transport_error", method=method, path=path, request=body,
                            request_key=key, exception=type(exc).__name__, message=str(exc))
            raise
        try:
            payload = response.json()
        except ValueError:
            payload = {"non_json": response.text[:1000]}
        self.log.record("http", method=method, path=path, request=body, request_key=key,
                        status=response.status_code, response=payload, elapsed_ms=round((time.monotonic()-started)*1000))
        assert response.status_code in expected, f"{method} {path}: HTTP {response.status_code}: {payload}"
        return payload

    async def eventually(self, check, *, timeout=None, description="condition"):
        deadline = time.monotonic() + (timeout or self.args.planning_timeout)
        last = None
        while time.monotonic() < deadline:
            last = await check()
            if last:
                return last
            await asyncio.sleep(.3)
        raise TimeoutError(f"timed out waiting for {description}; last={last}")

    async def connected(self, mode):
        body = await self.request("GET", API + "/robots/" + quote(self.creds["binding_id"]))
        if not body["robot"]["connected"]:
            return False
        if mode == "unknown":
            return body
        ready = (body.get("snapshot") or {}).get("readiness", {})
        return body if ready.get("busy") is False and "nav_server_ready" in ready else False

    async def get(self, task_id):
        value = await self.request("GET", API + "/tasks/" + task_id)
        self.tasks[task_id] = value
        return value

    async def wait_task(self, task_id, predicate, *, timeout=None):
        async def check():
            task = await self.get(task_id)
            return task if predicate(task) else False
        return await self.eventually(check, timeout=timeout, description=f"task {task_id}")

    async def submit(self, text=GOAL, key=None, token=None):
        key = key or "accept-" + uuid4().hex
        task = await self.request("POST", API + "/tasks", body={"text": text}, key=key,
                                  token=token, expected=(202,))
        self.tasks[task["task_id"]] = task
        self.task_meta[task["task_id"]] = {"case": self.log.current, "request_key": key, "text": text}
        return task, key

    async def planned(self, text=GOAL, *, min_steps=2):
        task, key = await self.submit(text)
        task = await self.wait_task(task["task_id"], lambda item: item["state"] in STABLE)
        if task["state"] != "WAITING_APPROVAL":
            raise NotVerified(f"real Luna did not yield an approval plan: {task['state']} / {task.get('reason_code')}")
        if len(task.get("plan_steps", [])) < min_steps:
            raise NotVerified(f"real Luna produced fewer than {min_steps} steps; no fabricated plan substituted")
        return task, key

    async def approve(self, task, *, expected=(202,), token=None):
        return await self.request("POST", API + f"/tasks/{task['task_id']}/decisions",
                                  body={"plan_id": task.get("plan_id") or "missing-plan",
                                        "decision": "approve", "expected_version": task["version"]},
                                  key="approve-" + uuid4().hex, token=token, expected=expected)

    async def register_resources(self):
        for kind, entries in (("environment", {point: point for point in WAYPOINTS}),
                              ("capabilities", {SKILL: "仅协议模拟的验收拿取技能"})):
            path = API + "/resources/" + kind
            query = "?" + urlencode({"binding_id": self.creds["binding_id"]})
            current = await self.request("GET", path + query)
            if current and current.get("entries") == entries:
                continue
            await self.request("POST", path + "/versions" + query,
                               body={"entries": entries, "source_refs": ["test:no-retry-real-http-ws-acceptance"],
                                     "base_version": current["revision"] if current else 0}, expected=(201,))

    def stopped_contract(self, task):
        assert task.get("advancement_stopped") is True, task
        error = task.get("error")
        assert isinstance(error, dict), f"structured error missing: {task}"
        assert error.get("code") and error.get("message") and error.get("source") and error.get("phase"), error
        assert any("\u4e00" <= char <= "\u9fff" for char in error["message"]), error
        assert "execution_status" in error, error

    async def no_more_commands(self, robot, count, duration=None):
        await asyncio.sleep(duration or self.args.observe_seconds)
        assert len(robot.commands) == count, f"unexpected dispatch: {robot.commands}"

    async def complete(self, robot, task):
        await self.approve(task)
        for _ in task["plan_steps"]:
            command = await robot.next_command()
            await robot.status(command, "running")
            await robot.status(command, "succeeded")
        return await self.wait_task(task["task_id"], lambda item: item["state"] == "SUCCEEDED")

    async def freeze_checks(self, robot, task, key):
        before = len(robot.commands)
        events_before = await self.request("GET", API + f"/tasks/{task['task_id']}/events")
        self.stopped_contract(task)
        lookup = await self.request("GET", API + "/task-requests/" + quote(key))
        assert lookup.get("found") is True and lookup["task"]["task_id"] == task["task_id"], lookup
        replay = await self.request("POST", API + "/tasks", body={"text": self.task_meta[task["task_id"]]["text"]},
                                    key=key, expected=(202,))
        assert replay["task_id"] == task["task_id"] and replay["state"] == task["state"], replay
        await self.approve(await self.get(task["task_id"]), expected=(409,))
        await self.no_more_commands(robot, before)
        current = await self.get(task["task_id"])
        assert current["state"] == task["state"] and current.get("advancement_stopped") is True, current
        events_after = await self.request("GET", API + f"/tasks/{task['task_id']}/events")
        previous_seq = events_before["next_seq"]
        assert not any(event.get("event_type", "").startswith(("planning", "planner_request"))
                       and event.get("seq", 0) > previous_seq for event in events_after["events"]), events_after

    async def case_success(self):
        async with RobotPeer(self) as robot:
            task, _ = await self.planned()
            final = await self.complete(robot, task)
            assert len(robot.commands) == len(task["plan_steps"])
            assert final.get("advancement_stopped") is False

    async def case_failed(self):
        async with RobotPeer(self) as robot:
            task, key = await self.planned()
            await self.approve(task)
            command = await robot.next_command()
            await robot.status(command, "failed", "测试执行器明确报告动作失败")
            task = await self.wait_task(task["task_id"], lambda row: row["state"] in {"FAILED", "BLOCKED"})
            await self.freeze_checks(robot, task, key)
            assert len(robot.commands) == 1

    async def timeout_task(self, robot):
        task, key = await self.planned()
        await self.approve(task)
        command = await robot.next_command()
        timeout = float(self.creds.get("command_timeout_seconds", 30)) + 20
        task = await self.wait_task(task["task_id"], lambda row: row.get("advancement_stopped") is True,
                                    timeout=timeout)
        assert any(row.get("outcome") == "RESULT_UNKNOWN" for row in task.get("result_summary", [])), task
        return task, key, command

    async def case_timeout(self):
        async with RobotPeer(self) as robot:
            task, key, _ = await self.timeout_task(robot)
            await self.freeze_checks(robot, task, key)
            assert len(robot.commands) == 1

    async def case_late_success(self):
        async with RobotPeer(self) as robot:
            task, key, command = await self.timeout_task(robot)
            await robot.status(command, "succeeded", "超时后才到达的成功证据")
            task = await self.wait_task(task["task_id"], lambda row: any(
                item.get("outcome") == "SUCCEEDED" for item in row.get("result_summary", [])))
            assert task["state"] != "SUCCEEDED", task
            await self.freeze_checks(robot, task, key)
            assert len(robot.commands) == 1

    async def case_duplicate(self):
        async with RobotPeer(self) as robot:
            key = "accept-duplicate-" + uuid4().hex
            results = await asyncio.gather(*(self.request("POST", API + "/tasks", body={"text": GOAL},
                                                         key=key, expected=(202,)) for _ in range(2)))
            assert results[0]["task_id"] == results[1]["task_id"], results
            task_id = results[0]["task_id"]
            self.task_meta[task_id] = {"case": self.log.current, "request_key": key, "text": GOAL}
            task = await self.wait_task(task_id, lambda row: row["state"] in STABLE)
            if task["state"] != "WAITING_APPROVAL":
                raise NotVerified(f"real Luna prerequisite unavailable: {task['state']}")
            lookup = await self.request("GET", API + "/task-requests/" + quote(key))
            assert lookup["found"] and lookup["task"]["task_id"] == task_id
            absent = await self.request("GET", API + "/task-requests/missing-" + uuid4().hex)
            assert absent["found"] is False, absent
            final = await self.complete(robot, task)
            replay = await self.request("POST", API + "/tasks", body={"text": GOAL}, key=key, expected=(202,))
            assert replay["task_id"] == task_id and replay["state"] == final["state"]
            await self.no_more_commands(robot, len(task["plan_steps"]))
            events = await self.request("GET", API + f"/tasks/{task_id}/events")
            assert sum(row["event_type"] == "planner_request_started" for row in events["events"]) == 1, events

    async def case_cancel(self):
        async with RobotPeer(self) as robot:
            task, _ = await self.planned()
            await self.approve(task)
            command = await robot.next_command()
            await robot.status(command, "running")
            # ws.send() acknowledges only the local write. Both send_observed and
            # robot_feedback can still advance the task version asynchronously.
            # Poll facts before the first cancel; never retry a rejected POST.
            last_ready_version = None

            async def written_and_running():
                nonlocal last_ready_version
                current = await self.get(task["task_id"])
                ready = (current["state"] == "RUNNING" and not current.get("advancement_stopped")
                         and any(row["command_id"] == command["cmd_id"]
                                 and row["dispatch_state"] == "SENT" and row["outcome"] == "RUNNING"
                                 for row in current["result_summary"]))
                stable = ready and last_ready_version == current["version"]
                last_ready_version = current["version"] if ready else None
                return current if stable else False

            task = await self.eventually(written_and_running, timeout=5,
                                         description="persisted send + running receipt and stable version before first cancel")
            self.log.record("cancel_precondition", task_id=task["task_id"], version=task["version"],
                            command_id=command["cmd_id"], dispatch_state="SENT", outcome="RUNNING",
                            cancel_posts_so_far=0)
            first = await self.request("POST", API + f"/tasks/{task['task_id']}/cancel",
                                       body={"expected_version": task["version"]}, key="cancel-" + uuid4().hex)
            await self.eventually(lambda: self.cancel_seen(robot), timeout=10, description="one cancel frame")
            second = await self.request("POST", API + f"/tasks/{task['task_id']}/cancel",
                                        body={"expected_version": first["version"]}, key="cancel-new-" + uuid4().hex)
            assert second.get("cancel_result") == first.get("cancel_result"), (first, second)
            await self.no_more_commands(robot, 1)
            assert sum(frame["type"] == "cancel" for frame in robot.received) == 1, robot.received
            await robot.status(command, "failed", "协议模拟器收到取消，报告动作未完成")
            final = await self.wait_task(task["task_id"], lambda row: any(
                item["command_id"] == command["cmd_id"] and item["outcome"] == "FAILED"
                for item in row["result_summary"]))
            assert final["state"] == "CANCELLED" and final.get("error") is None, final

    async def cancel_seen(self, robot):
        return any(frame["type"] == "cancel" for frame in robot.received)

    async def case_no_robot(self):
        robots = await self.request("GET", API + "/robots")
        if any(row.get("connected") for row in robots):
            raise NotVerified("another test robot is connected; no-robot prerequisite is false")
        task, key = await self.submit()
        task = await self.wait_task(task["task_id"], lambda row: row["state"] == "BLOCKED")
        assert task["reason_code"] == "NO_ONLINE_ROBOT", task
        self.stopped_contract(task)
        replay = await self.request("POST", API + "/tasks", body={"text": GOAL}, key=key, expected=(202,))
        assert replay["task_id"] == task["task_id"] and replay["state"] == "BLOCKED"

    async def unavailable_robot(self, mode, reason):
        async with RobotPeer(self, mode) as robot:
            task, key = await self.submit()
            task = await self.wait_task(task["task_id"], lambda row: row["state"] in {"BLOCKED", "FAILED"})
            assert task["reason_code"] == reason, task
            await self.freeze_checks(robot, task, key)
            assert not robot.commands

    async def case_state_unknown(self):
        await self.unavailable_robot("unknown", "ROBOT_STATE_UNKNOWN")

    async def case_not_ready(self):
        await self.unavailable_robot("not_ready", "READINESS_UNKNOWN")

    async def case_queue(self):
        async with RobotPeer(self) as robot:
            first, _ = await self.planned()
            await self.approve(first)
            command = await robot.next_command()
            await robot.status(command, "running")
            second, _ = await self.submit(f"请移动到{WAYPOINTS[1]}。")
            await self.wait_task(second["task_id"], lambda row: row["state"] == "QUEUED")
            await robot.status(command, "succeeded")
            for _ in first["plan_steps"][1:]:
                await robot.status(await robot.next_command(), "succeeded")
            await self.wait_task(first["task_id"], lambda row: row["state"] == "SUCCEEDED")
            second = await self.wait_task(second["task_id"], lambda row: row["state"] in STABLE)
            if second["state"] != "WAITING_APPROVAL":
                raise NotVerified(f"queued real planning prerequisite: {second['state']}")
            await self.complete(robot, second)

    async def case_concurrent_reads(self):
        """Exercise real read/write overlap; no mock service or request retry."""
        counts = {"history": 0, "home": 0, "health": 0}
        stopped = asyncio.Event()

        async def health():
            value = await self.request("GET", "/health")
            assert value.get("configured") is True and value.get("execution_enabled") is True, value
            counts["health"] += 1

        async def read_loop(name, path, delay):
            while not stopped.is_set():
                if name == "health":
                    await health()
                else:
                    await self.request("GET", path)
                    counts[name] += 1
                await asyncio.sleep(delay)

        await health()
        async with RobotPeer(self) as robot:
            async def execute():
                task, _ = await self.planned()
                await self.approve(task)
                for _ in task["plan_steps"]:
                    command = await robot.next_command()
                    await robot.status(command, "running")
                    # Keep genuine running feedback/reads overlapping before the
                    # simulated executor reports its genuine protocol terminal.
                    await asyncio.sleep(.5)
                    await robot.status(command, "succeeded")
                await self.wait_task(task["task_id"], lambda row: row["state"] == "SUCCEEDED")
                assert len(robot.commands) == len(task["plan_steps"])

            execution = asyncio.create_task(execute())
            readers = [asyncio.create_task(read_loop("history", API + "/tasks?limit=100", .1)),
                       asyncio.create_task(read_loop("home", API + "/home-tasks", .1)),
                       asyncio.create_task(read_loop("health", "/health", .2))]
            try:
                done, _ = await asyncio.wait([execution, *readers], return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    await job
                assert execution in done, "a concurrent reader stopped before task completion"
            finally:
                stopped.set()
                if not execution.done():
                    execution.cancel()
                outcomes = await asyncio.gather(execution, *readers, return_exceptions=True)
                self.log.record("concurrent_read_counts", counts=counts,
                                request_retries=0, database_deadlock_delta="root checks isolated DB separately")
                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        raise outcome
        await health()
        assert counts["history"] > 0 and counts["home"] > 0 and counts["health"] > 2, counts
        self.log.record("concurrent_read_counts", counts=counts,
                        request_retries=0, database_deadlock_delta="root checks isolated DB separately")

    async def case_clarification(self):
        async with RobotPeer(self) as robot:
            task, _ = await self.submit("请帮我移动，目的地还需要我补充。")
            task = await self.wait_task(task["task_id"], lambda row: row["state"] in STABLE)
            if task["state"] != "NEEDS_CLARIFICATION":
                raise NotVerified(f"Luna did not ask a clarification: {task['state']}")
            assert not robot.commands
            answered = await self.request("POST", API + f"/tasks/{task['task_id']}/clarifications/{task['clarification_id']}/answer",
                                         body={"answer": f"目的地已确定为{WAYPOINTS[0]}，请按此目的地执行移动。",
                                               "expected_version": task["version"]},
                                         key="answer-" + uuid4().hex, expected=(202,))
            task = await self.wait_task(task["task_id"], lambda row: row["state"] in STABLE and
                                       row["version"] > answered["version"])
            if task["state"] != "WAITING_APPROVAL":
                raise NotVerified(f"Luna did not plan after clarification: {task['state']}")
            await self.complete(robot, task)

    async def changed_resource(self, kind, expired=False):
        async with RobotPeer(self) as robot:
            goal = GOAL if kind == "waypoint" else f"请在当前位置执行技能 {SKILL}。"
            task, _ = await self.planned(goal, min_steps=2 if kind == "waypoint" else 1)
            if kind == "skill" and not any(row["action"] == "vla_skill" for row in task["plan_steps"]):
                raise NotVerified("Luna did not use the real registered test skill")
            if expired:
                robot.expire_resources = True
                await asyncio.sleep(float(self.creds.get("freshness_seconds", 15)) + 1)
            else:
                if kind == "waypoint":
                    robot.waypoints = []
                else:
                    robot.skills = []
                await robot.report()
            # The HTTP request may acknowledge a persisted BLOCKED decision or reject it.
            await self.approve(await self.get(task["task_id"]), expected=(202, 409, 422))
            task = await self.wait_task(task["task_id"], lambda row: row.get("advancement_stopped") is True)
            self.stopped_contract(task)
            expected_resource_code = "WAYPOINT_UNVERIFIED" if kind == "waypoint" else "SKILL_UNVERIFIED"
            assert task["error"]["source"] == "resources" and task["error"]["code"] in {
                expected_resource_code, "RESOURCE_EXPIRED", "RESOURCE_VERSION_CHANGED"}, task
            await self.no_more_commands(robot, 0)

    async def case_waypoint_removed(self):
        await self.changed_resource("waypoint")

    async def case_skill_removed(self):
        await self.changed_resource("skill")

    async def case_waypoint_expired(self):
        await self.changed_resource("waypoint", expired=True)

    async def case_skill_expired(self):
        await self.changed_resource("skill", expired=True)

    async def missing_resource(self, goal, expected_code):
        async with RobotPeer(self) as robot:
            task, _ = await self.submit(goal)
            task = await self.wait_task(task["task_id"], lambda row: row["state"] in STABLE)
            await self.no_more_commands(robot, 0)
            if task["state"] == "WAITING_APPROVAL":
                raise AssertionError("nonexistent resource request yielded an executable approval plan")
            self.log.record("scope", explanation="Missing-resource input used real Luna; a model clarification/unsupported result does not exercise deterministic invalid-plan validation.")
            if task.get("reason_code") != expected_code:
                raise NotVerified(f"real Luna safely stopped at {task['state']} / {task.get('reason_code')}; "
                                  f"the specific {expected_code} validator path was not exercised")

    async def case_missing_waypoint(self):
        await self.missing_resource("请移动到并不存在的地点：验收不存在Z点。不得改用其他地点。", "WAYPOINT_UNVERIFIED")

    async def case_missing_skill(self):
        await self.missing_resource("请执行并不存在的技能 test_no_retry_nonexistent_skill，不得替换为其他技能。", "SKILL_UNVERIFIED")

    async def case_permission_denied(self):
        token = self.creds.get("unprivileged_token")
        if not token:
            raise NotVerified("credentials file has no unprivileged_token")
        async with RobotPeer(self) as robot:
            task, _ = await self.planned()
            await self.approve(task, token=token, expected=(403,))
            await self.no_more_commands(robot, 0)
            current = await self.get(task["task_id"])
            assert current["state"] == "WAITING_APPROVAL", current
            await self.complete(robot, current)

    async def case_ui_approval_hold(self):
        token = self.creds.get("unprivileged_token")
        if not token:
            raise NotVerified("UI approval verification requires unprivileged_token")
        async with RobotPeer(self) as robot:
            task, _ = await self.submit(GOAL, token=token)
            task = await self.wait_task(task["task_id"], lambda row: row["state"] in STABLE)
            if task["state"] != "WAITING_APPROVAL":
                raise NotVerified(f"real unprivileged-user task did not reach approval: {task['state']}")
            stop_file = self.log.directory / "ui-approval-release.flag"
            metadata = {"task_id": task["task_id"], "state": task["state"],
                        "actor_credential_field": "unprivileged_token", "base_url": self.base,
                        "release_flag": str(stop_file), "commands_allowed": False}
            self.log.save("ui-approval-ready.json", metadata)
            self.log.record("ui_approval_ready", **metadata)
            print(json.dumps({"ui_approval_ready": metadata}, ensure_ascii=False), flush=True)
            deadline = time.monotonic() + self.args.ui_hold_seconds
            while not stop_file.exists() and time.monotonic() < deadline:
                assert not robot.commands, "an unprivileged UI approval unexpectedly dispatched a command"
                await asyncio.sleep(.2)
            assert not robot.commands, "the UI approval hold dispatched a command"
            if not stop_file.exists():
                raise NotVerified("UI observation deadline elapsed before root released the protocol peer")
            final = await self.get(task["task_id"])
            assert final["state"] == "WAITING_APPROVAL", final

    async def case_model_failure(self):
        if self.base != "http://127.0.0.1:18874":
            raise NotVerified("model_failure must use separate :18874 instance with genuinely unreachable provider URL")
        if self.creds.get("model_failure_instance") is not True:
            raise NotVerified("credentials must attest model_failure_instance=true; no model response is fabricated")
        async with RobotPeer(self) as robot:
            task, key = await self.submit()
            task = await self.wait_task(task["task_id"], lambda row: row["state"] in {"BLOCKED", "FAILED"})
            assert task["reason_code"] == "PLANNER_UNAVAILABLE", task
            await self.freeze_checks(robot, task, key)
            assert not robot.commands

    async def case_disconnect_unknown(self):
        async with RobotPeer(self) as robot:
            task, key = await self.planned()
            await self.approve(task)
            await robot.next_command()
            await robot.disconnect()
            task = await self.wait_task(task["task_id"], lambda row: row.get("advancement_stopped") is True)
            self.stopped_contract(task)
            assert any(row["outcome"] == "RESULT_UNKNOWN" for row in task["result_summary"]), task
        async with RobotPeer(self) as robot:
            await self.freeze_checks(robot, task, key)
            assert not robot.commands

    async def case_conflict(self):
        async with RobotPeer(self) as robot:
            task, key = await self.planned()
            await self.approve(task)
            command = await robot.next_command()
            await robot.status(command, "failed")
            await self.wait_task(task["task_id"], lambda row: row.get("advancement_stopped") is True)
            await robot.status(command, "succeeded", "测试互相冲突的终态证据")
            task = await self.wait_task(task["task_id"], lambda row: row.get("result_disputed") is True)
            await self.freeze_checks(robot, task, key)
            assert len(robot.commands) == 1

    async def case_send_timeout(self):
        raise NotVerified("A deterministic TCP/WS write stall cannot be created by this protocol-only peer. "
                          "Use an explicitly controlled isolated transport failure; do not substitute a mocked send result. "
                          "The timeout case verifies missing robot receipts, not a socket write timeout.")

    async def case_untracked(self):
        async with RobotPeer(self) as robot:
            task, _ = await self.planned()
            await robot.send({"type": "hello", "robot_id": self.creds["robot_id"],
                              "executor": "no-retry-acceptance-protocol-simulator",
                              "busy_cmd_id": "test-untracked-" + uuid4().hex,
                              "skills": ["move_to", "vla_skill"]})
            async def isolated():
                row = await self.request("GET", API + "/robots/" + quote(self.creds["binding_id"]))
                return row if row["robot"].get("isolation_reason") else False
            await self.eventually(isolated, timeout=10, description="untracked feedback isolates test binding")
            task = await self.wait_task(task["task_id"], lambda row: row.get("advancement_stopped") is True)
            assert task["reason_code"] == "UNTRACKED_EXECUTION", task
            self.stopped_contract(task)
            await self.approve(task, expected=(409,))
            await self.no_more_commands(robot, 0)
            await self.get(task["task_id"])

    async def case_restart_prepare(self):
        async with RobotPeer(self) as robot:
            task, key = await self.planned()
            await self.approve(task)
            command = await robot.next_command()
            await robot.status(command, "running")
            task = await self.get(task["task_id"])
            self.log.save("restart-checkpoint.json", {"base_url": self.base, "task_id": task["task_id"],
                                                      "request_key": key, "command": command,
                                                      "text": GOAL, "prepared_at": utc()})
            self.log.record("scope", explanation="Checkpoint only. Root must restart the isolated release, then run restart_verify. Disconnect alone is not a restart verification.")
        raise NotVerified("restart checkpoint prepared; restart_verify after actual isolated process restart is required")

    async def case_restart_verify(self):
        if not self.args.checkpoint:
            raise NotVerified("--checkpoint from restart_prepare is required")
        checkpoint = json.loads(Path(self.args.checkpoint).read_text(encoding="utf-8"))
        assert checkpoint["base_url"] == self.base
        task = await self.get(checkpoint["task_id"])
        assert task["state"] == "CANCELLED" and task["reason_code"] == "RESTART_TASK_LOST", task
        self.task_meta[task["task_id"]] = {"case": self.log.current, "request_key": checkpoint["request_key"],
                                           "text": checkpoint["text"]}
        async with RobotPeer(self) as robot:
            await robot.status(checkpoint["command"], "succeeded", "真正重启后的迟到成功证据")
            await self.no_more_commands(robot, 0)
            task = await self.get(task["task_id"])
            assert task["state"] == "CANCELLED" and task.get("advancement_stopped") is True, task
            replay = await self.request("POST", API + "/tasks", body={"text": checkpoint["text"]},
                                        key=checkpoint["request_key"], expected=(202,))
            assert replay["task_id"] == task["task_id"] and replay["state"] == "CANCELLED", replay
            await self.no_more_commands(robot, 0)

    async def capture(self, case):
        for task_id, meta in list(self.task_meta.items()):
            if meta["case"] != case:
                continue
            try:
                task = await self.get(task_id)
                events, after = [], 0
                while True:
                    page = await self.request("GET", API + f"/tasks/{task_id}/events?after={after}")
                    events.extend(page["events"])
                    if not page["events"] or page["next_seq"] == after:
                        break
                    after = page["next_seq"]
                self.log.save(f"{case}-{task_id}.json", {"task": task, "events": events,
                                                        "robot_evidence_ids": [e for row in task.get("result_summary", [])
                                                                               for e in row.get("evidence_ids", [])], **meta})
                self.task_event_counts[task_id] = dict(Counter(row["event_type"] for row in events))
            except Exception as exc:
                self.log.record("capture_error", task_id=task_id, exception=type(exc).__name__, message=str(exc))

    async def run(self):
        try:
            self.log.record("scope", base_url=self.base, robot_id=self.creds["robot_id"],
                            source_release=self.creds.get("source_release"),
                            real_components=["HTTP API", "TaskService", "PostgreSQL", "configured real Luna planner"],
                            simulated_components=["dlv/0 robot protocol peer only"],
                            exclusions=["physical robot execution", "production :18763", "robot .39", "robot .9"],
                            provider_call_count="requires independent server/provider evidence; not inferable from public API")
            await self.register_resources()
            for case in self.args.cases.split(","):
                self.log.current = case
                before = dict(self.log.requests)
                started = time.monotonic()
                try:
                    await getattr(self, "case_" + case)()
                    result = {"case": case, "status": "PASS"}
                except NotVerified as exc:
                    result = {"case": case, "status": "NOT_VERIFIED", "reason": str(exc)}
                except Exception as exc:
                    result = {"case": case, "status": "FAIL", "exception": type(exc).__name__, "reason": str(exc)}
                await self.capture(case)
                result.update({"elapsed_seconds": round(time.monotonic()-started, 2),
                               "task_ids": [key for key, meta in self.task_meta.items() if meta["case"] == case],
                               "planner_invocation_count": sum(self.task_event_counts.get(key, {}).get("planner_request_started", 0)
                                                               for key, meta in self.task_meta.items() if meta["case"] == case),
                               "http_request_count": sum(self.log.requests.values()) - sum(before.values()),
                               "command_frames": [row["frame"] for row in self.log.records if row["case"] == case
                                                  and row["kind"] == "ws_receive" and row["frame"].get("type") == "command"],
                               "cancel_frames": [row["frame"] for row in self.log.records if row["case"] == case
                                                 and row["kind"] == "ws_receive" and row["frame"].get("type") == "cancel"]})
                concurrency = [row for row in self.log.records if row["case"] == case
                               and row["kind"] == "concurrent_read_counts"]
                if concurrency:
                    result["concurrent_reads"] = concurrency[-1]["counts"]
                self.results.append(result)
                self.log.record("case_result", **result)
                self.write_summary()
                print(json.dumps(self.log.clean(result), ensure_ascii=False), flush=True)
                if result["status"] == "FAIL" and not self.args.continue_on_failure:
                    break
                if case in {"timeout", "disconnect_unknown", "conflict", "untracked"}:
                    self.log.record("scope", explanation="Unresolved command or binding isolation may affect later cases; use a fresh isolated schema or run cleanup/restart explicitly.")
        finally:
            await self.client.aclose()
            self.write_summary()
        return 1 if any(row["status"] == "FAIL" for row in self.results) else 2 if any(
            row["status"] == "NOT_VERIFIED" for row in self.results) else 0

    def write_summary(self):
        executed = {row["case"] for row in self.results}
        self.log.save("summary.json", {"base_url": self.base, "results": self.results,
                                        "not_run": [case for case in CASES if case not in executed],
                                        "http_requests": dict(self.log.requests),
                                        "task_event_counts": self.task_event_counts,
                                        "planner_invocation_count": sum(row.get("planner_request_started", 0)
                                                                        for row in self.task_event_counts.values()),
                                        "provider_request_count": None,
                                        "provider_count_limitation": "planner_request_started counts persisted starts of the real planner call. Actual provider HTTP requests need independent server/provider evidence.",
                                        "screenshot_tasks": [{"task_id": task_id, **meta} for task_id, meta in self.task_meta.items()],
                                        "credentials_path": str(Path(self.args.credentials_path).resolve()),
                                        "scope": "Real release HTTP + real configured model/TaskService/Postgres; only labelled robot protocol is simulated."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, type=safe_base_url)
    parser.add_argument("--credentials-path", required=True)
    parser.add_argument("--evidence-dir", default="D:/codex_chat/agent-platform/operations/no-retry-20261106")
    parser.add_argument("--cases", default="success,duplicate,cancel,no_robot,state_unknown,not_ready,failed,late_success")
    parser.add_argument("--planning-timeout", type=float, default=120)
    parser.add_argument("--observe-seconds", type=float, default=3)
    parser.add_argument("--checkpoint")
    parser.add_argument("--ui-hold-seconds", type=float, default=600)
    parser.add_argument("--continue-on-failure", action="store_true")
    args = parser.parse_args()
    selected = args.cases.split(",")
    if any(case not in CASES for case in selected):
        parser.error("unknown case; choices: " + ",".join(CASES))
    if len(set(selected)) != len(selected):
        parser.error("case names must be unique per run")
    suite = Suite(args)
    print("Evidence directory: " + str(suite.log.directory), flush=True)
    return asyncio.run(suite.run())


if __name__ == "__main__":
    sys.exit(main())
