"""Protocol-only assistant for manual continuation UI acceptance.

The browser submits and approves all tasks. This helper permits only HTTP GETs,
never imports robot_agent, never writes a database, and never supplies LLM output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time
from urllib.parse import urlencode

if __package__:
    from .real_no_retry_acceptance import (
        API, SKILL, WAYPOINTS, Evidence, RobotPeer, Suite, load_credentials, safe_base_url, utc,
    )
else:
    from real_no_retry_acceptance import (
        API, SKILL, WAYPOINTS, Evidence, RobotPeer, Suite, load_credentials, safe_base_url, utc,
    )


EVIDENCE_ROOT = Path(r"D:\codex_chat\agent-platform\operations\no-retry-20261106\continuation\peer")
LIVE_STATES = {"RECEIVED", "PLANNING", "NEEDS_CLARIFICATION", "WAITING_APPROVAL",
               "READY", "QUEUED", "RUNNING", "CANCEL_REQUESTED"}
OUTCOMES = ("succeeded", "succeeded", "failed", "succeeded")


def continuation_base(value):
    checked = safe_base_url(value)
    if checked != "http://127.0.0.1:18873":
        raise ValueError("continuation UI helper permits only http://127.0.0.1:18873")
    return checked


def permitted_command(frame):
    args = frame.get("args", {})
    return bool(frame.get("cmd_id")) and (
        (frame.get("skill") == "move_to" and args.get("location") in WAYPOINTS)
        or (frame.get("skill") == "vla_skill" and args.get("skill_id") == SKILL)
    )


def task_blocks_start(task):
    return bool(task.get("allocation_robot_binding_id")) or (
        task.get("state") in LIVE_STATES and not task.get("advancement_stopped")
    )


class ReadOnlySuite(Suite):
    async def request(self, method, path, **kwargs):
        if method != "GET" or not path.startswith(API + "/"):
            raise ValueError("UI peer cannot submit, approve, cancel, or modify resources over HTTP")
        return await super().request(method, path, **kwargs)


async def inspect_existing(suite):
    """Do not connect the robot while an old task may claim it automatically."""
    identities = []
    blockers = []
    for token_name in ("web_token", "unprivileged_token"):
        token = suite.creds.get(token_name)
        if not token:
            continue
        identity = await suite.request("GET", API + "/session", token=token)
        if not str(identity.get("actor_id", "")).startswith("test-no-retry-"):
            raise ValueError("only test-no-retry- user identities are permitted")
        # The public session response exposes actor_id only. Customer/database
        # isolation remains the verified launcher's prerequisite, not a fake API check.
        identities.append({"actor_id": identity["actor_id"]})
        cursor = None
        for _ in range(100):
            query = {"limit": 100}
            if cursor is not None:
                query["before"] = cursor
            history = await suite.request("GET", API + "/tasks?" + urlencode(query), token=token)
            blockers.extend({"actor_id": identity["actor_id"], **task}
                            for task in history["tasks"] if task_blocks_start(task))
            cursor = history.get("next_before")
            if cursor is None:
                break
        else:
            raise ValueError("task history exceeded checked pages; inspection incomplete")
    robots = await suite.request("GET", API + "/robots")
    if not robots or any(not str(robot.get(key, "")).startswith("test-no-retry-")
                         for robot in robots for key in ("binding_id", "wire_robot_id")):
        raise ValueError("non-test robot registration detected")
    target = [robot for robot in robots if robot["binding_id"] == suite.creds["binding_id"]]
    if len(target) != 1 or target[0]["wire_robot_id"] != suite.creds["robot_id"]:
        raise ValueError("test robot binding differs from provided credentials")
    if target[0].get("connected") or target[0].get("isolation_reason"):
        raise ValueError("test robot already connected or isolated; operator decision required")
    suite.log.save("preflight.json", {"at": utc(), "identities": identities, "robots": robots,
                                      "blocking_tasks": blockers,
                                      "scope": "all pages for both supplied test users; no database writes"})
    if blockers:
        raise ValueError("existing unfinished/allocated test tasks found; inspect preflight.json before connecting")


async def run(args):
    args.base_url = continuation_base(args.base_url)
    load_credentials(args.credentials_path)  # Fail before Suite opens any network client.
    if not 1 <= args.max_duration <= 1200 or not 0 <= args.command_delay <= 30:
        raise ValueError("duration must be 1..1200 seconds and command delay 0..30 seconds")
    args.evidence_dir = str(EVIDENCE_ROOT)
    args.planning_timeout = 30
    suite = ReadOnlySuite(args)
    suite.log.current = "continuation-ui"
    status = "NOT_VERIFIED"
    reason = "helper not started"
    peer = None
    terminal_job = None
    terminal_results = []
    processed_commands = 0
    stop_flag = EVIDENCE_ROOT / "stop.flag"
    shared = Evidence(EVIDENCE_ROOT, suite.log.secrets)
    ready = {"ready": False, "base_url": args.base_url, "evidence_directory": str(suite.log.directory),
             "stop_flag": str(stop_flag), "simulated_robot": True, "at": utc()}
    shared.save("ready.json", ready)
    try:
        if stop_flag.exists():
            raise ValueError("stop.flag already exists; operator must remove it before a new run")
        await inspect_existing(suite)
        if args.inspect_only:
            status, reason = "INSPECTED", "read-only preflight passed; WebSocket was not connected"
            return 0
        async with RobotPeer(suite) as peer:
            ready.update(ready=True, at=utc(), planned_command_results=list(OUTCOMES))
            suite.log.save("ready.json", ready)
            shared.save("ready.json", ready)
            print("CONTINUATION_PEER_READY=" + json.dumps(ready, ensure_ascii=True), flush=True)
            deadline = time.monotonic() + args.max_duration

            async def finish(command, outcome):
                await asyncio.sleep(args.command_delay)
                await peer.status(command, outcome, "UI continuation protocol simulation: " + outcome)
                terminal_results.append({"cmd_id": command["cmd_id"], "state": outcome, "at": utc()})
                suite.log.save("progress.json", {"command_count": len(peer.commands),
                                                  "terminal_results": terminal_results,
                                                  "cancel_count": sum(f["type"] == "cancel" for f in peer.received)})

            while time.monotonic() < deadline:
                if stop_flag.exists():
                    status = "PASS" if (len(peer.commands) == 4
                                         and tuple(r["state"] for r in terminal_results) == OUTCOMES
                                         and not any(r.get("cancelled") for r in terminal_results)) else "NOT_VERIFIED"
                    reason = "operator stop flag observed"
                    break
                if terminal_job and terminal_job.done():
                    await terminal_job
                    terminal_job = None
                if peer.ws is None or any(job.done() for job in peer.jobs):
                    raise RuntimeError("protocol connection/reporting ended unexpectedly")
                try:
                    frame = await asyncio.wait_for(peer.queue.get(), .3)
                except TimeoutError:
                    continue
                if frame["type"] == "command":
                    processed_commands += 1
                    index = processed_commands
                    if index > len(OUTCOMES) or peer.active or not permitted_command(frame):
                        suite.log.record("unexpected_command", frame=frame, command_count=index)
                        raise RuntimeError("unexpected, overlapping, or out-of-scope command; helper stops")
                    if terminal_job:
                        await terminal_job
                    peer.active = frame["cmd_id"]
                    await peer.status(frame, "running", "UI continuation protocol simulation: running")
                    terminal_job = asyncio.create_task(finish(frame, OUTCOMES[index - 1]))
                else:
                    suite.log.record("cancel_observed", frame=frame, active=peer.active)
                    if frame.get("cmd_id") != peer.active:
                        raise RuntimeError("cancel does not match active simulated command")
                    if terminal_job:
                        terminal_job.cancel()
                        await asyncio.gather(terminal_job, return_exceptions=True)
                        terminal_job = None
                    command = next(c for c in peer.commands if c["cmd_id"] == peer.active)
                    await peer.status(command, "failed", "UI continuation protocol simulation: cancelled")
                    terminal_results.append({"cmd_id": command["cmd_id"], "state": "failed", "cancelled": True, "at": utc()})
            else:
                reason = "maximum duration elapsed; operator completion was not confirmed"
            if terminal_job:
                terminal_job.cancel()
                await asyncio.gather(terminal_job, return_exceptions=True)
                terminal_job = None
    except Exception as exc:
        status, reason = "FAIL", str(exc)
        suite.log.record("helper_error", exception=type(exc).__name__, message=reason)
    finally:
        if terminal_job:
            terminal_job.cancel()
            await asyncio.gather(terminal_job, return_exceptions=True)
        await suite.client.aclose()
        frames = peer.received if peer else []
        summary = {"status": status, "reason": reason, "base_url": args.base_url, "simulated_robot": True,
                   "commands": [f for f in frames if f["type"] == "command"],
                   "command_count": sum(f["type"] == "command" for f in frames),
                   "cancel_count": sum(f["type"] == "cancel" for f in frames),
                   "terminal_results": terminal_results, "http_requests": dict(suite.log.requests),
                   "http_writes": 0, "database_writes": 0, "model_output_simulated": False,
                   "evidence_directory": str(suite.log.directory), "at": utc()}
        suite.log.save("summary.json", summary)
        shared.save("latest-summary.json", summary)
        shared.save("ready.json", {**ready, "ready": False, "status": status, "reason": reason, "at": utc()})
        print("CONTINUATION_PEER_RESULT=" + json.dumps(suite.log.clean(summary), ensure_ascii=True), flush=True)
    return 0 if status == "PASS" else 2 if status == "NOT_VERIFIED" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18873", type=continuation_base)
    parser.add_argument("--credentials-path", required=True)
    parser.add_argument("--max-duration", type=int, default=1200)
    parser.add_argument("--command-delay", type=float, default=2)
    parser.add_argument("--inspect-only", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
