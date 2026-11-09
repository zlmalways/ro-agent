"""Explicit live PCM recording verification, using the real local test platform.

Only the robot protocol input is simulated. ASR, planning, approval, dispatch,
and PostgreSQL persistence use the application itself. Stop at the first failure.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes as ct
from ctypes import wintypes as w
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.acceptance.verify_dev_launcher import (  # noqa: E402
    launch, listening, secret, stop_with_ctrl_c, wait_healthy,
)
from tests.manual.mock_robot_ws import Simulator, command_session, send_pcm  # noqa: E402

TEST = ROOT / "test-db"
APP_CONFIG = ROOT / "config/dev.e2e.local.json"
RUNTIME_CONFIG = TEST / "wav-e2e-runtime.json"
RECORDINGS = Path("D:/ro-agent-data/testdata/recordings")
URL = "ws://127.0.0.1:18764/api/voice/robot/ws"
REPORT = TEST / "wav-recording-verification.json"


def require(value, message):
    if not value:
        raise RuntimeError(message)


def read_asr_credential(target):
    class Credential(ct.Structure):
        _fields_ = [
            ("flags", w.DWORD), ("kind", w.DWORD), ("target", w.LPWSTR), ("comment", w.LPWSTR),
            ("written", w.FILETIME), ("size", w.DWORD), ("blob", ct.c_void_p), ("persist", w.DWORD),
            ("count", w.DWORD), ("attrs", ct.c_void_p), ("alias", w.LPWSTR), ("user", w.LPWSTR),
        ]
    api = ct.WinDLL("Advapi32", use_last_error=True)
    entry = ct.POINTER(Credential)()
    api.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ct.POINTER(ct.POINTER(Credential))]
    api.CredReadW.restype = w.BOOL
    api.CredFree.argtypes = [ct.c_void_p]
    if not api.CredReadW(target, 1, 0, ct.byref(entry)):
        raise RuntimeError("ASR Windows credential is unavailable")
    try:
        return ct.string_at(entry.contents.blob, entry.contents.size).decode("utf-16-le").strip()
    finally:
        api.CredFree(entry)


def load_provider_environment(path, credential):
    private = json.loads(path.read_text(encoding="utf-8"))
    selected = private.get("DEEPLEAP_LLM_PROVIDER", "").lower()
    require(selected in {"relay", "gpt", "deepseek"}, "Planner provider is not configured")
    prefix = selected.upper()
    names = ["DEEPLEAP_LLM_PROVIDER", "VOICE_ASR_PROVIDER", "VOICE_ASR_OPENAI_MODEL"]
    names += [f"{prefix}_{suffix}" for suffix in ("BASE_URL", "API_KEY", "MODEL", "TIMEOUT_S")]
    environment = {key: private[key] for key in names if key in private}
    require(environment.get("VOICE_ASR_PROVIDER") == "openai", "This run requires the approved OpenAI ASR")
    environment["VOICE_ASR_OPENAI_API_KEY"] = read_asr_credential(credential)
    require(environment.get(f"{prefix}_API_KEY") and environment["VOICE_ASR_OPENAI_API_KEY"],
            "Provider credentials are incomplete")
    return environment


async def get_json(client, path):
    response = await client.get(path)
    response.raise_for_status()
    return response.json()


async def task_result(client, task_id):
    approved = False
    async with asyncio.timeout(300):
        while True:
            task = await get_json(client, f"/api/robot-agent/tasks/{task_id}")
            if task["state"] == "WAITING_APPROVAL":
                require(not approved, "Task requested approval twice")
                require(task["plan_robot_binding_id"] == "pcm-mock-fridge-binding", "Wrong robot was selected")
                steps = task["plan_steps"]
                require(len(steps) == 1 and steps[0]["action"] == "move_to"
                        and steps[0]["args"] == {"location": "前台"}, "Unexpected real model plan")
                response = await client.post(f"/api/robot-agent/tasks/{task_id}/decisions",
                    headers={"idempotency-key": str(uuid4())},
                    json={"plan_id": task["plan_id"], "decision": "approve", "expected_version": task["version"]})
                response.raise_for_status()
                approved = True
            elif task["state"] == "SUCCEEDED":
                require(approved, "Task bypassed its configured approval policy")
                return task
            else:
                require(task["state"] in {"RECEIVED", "PLANNING", "READY", "QUEUED", "RUNNING"},
                        f"Task stopped: {task['state']} / {task['reason_code']}")
            await asyncio.sleep(0.25)


async def evidence(task_id, segment_id):
    db = await asyncpg.connect(host="127.0.0.1", port=55440, database="ro_agent_sim_test",
                              user="ro_agent_sim_test", password=secret("db-password"))
    try:
        inputs = await db.fetch("SELECT input_id,state,normalized_text,provider_ref,result_task_id FROM ra_inputs "
                                "WHERE key=$1", segment_id)
        commands = await db.fetch("SELECT command_id,dispatch_state,wire_skill,wire_args,outcome FROM ra_commands "
                                  "WHERE task_id=$1", task_id)
        require(len(inputs) == 1 and inputs[0]["state"] == "COMPLETE", "Input persistence mismatch")
        require(inputs[0]["provider_ref"] and inputs[0]["result_task_id"] == task_id, "Missing real ASR evidence")
        require(len(commands) == 1 and commands[0]["outcome"] == "SUCCEEDED"
                and commands[0]["dispatch_state"] == "SENT", "Command persistence mismatch")
        events = await db.fetch("SELECT payload FROM ra_robot_events WHERE cmd_id=$1 "
                                "AND event_type='cmd_status' ORDER BY received_at,event_id", commands[0]["command_id"])
        replies = [json.loads(row["payload"]) for row in events]
        require([row["state"] for row in replies] == ["running", "succeeded"], "Robot replies were not persisted")
        return {"inputs": [dict(row) for row in inputs], "commands": [dict(row) for row in commands],
                "robot_replies": replies}
    finally:
        await db.close()


async def run_case(name, fixture, output_root, base_config, runtime):
    config = json.loads(json.dumps(base_config))
    config["allow_external_calls"] = True
    config["deployment"]["execution_enabled"] = True
    config["voice_pcm"].update(enabled=True, evaluate_wake=True, submit_tasks=True)
    blocker = output_root / "not-a-directory"
    if name == "without-config":
        config["voice_pcm"].pop("wav_save_dir", None)
    elif name == "write-failure":
        blocker.write_text("Deliberately blocks only this acceptance recording directory.", encoding="utf-8")
        config["voice_pcm"]["wav_save_dir"] = str(blocker)
    else:
        config["voice_pcm"]["wav_save_dir"] = str(RECORDINGS)
    APP_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    runtime["application_config"] = str(APP_CONFIG)
    RUNTIME_CONFIG.write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
    before = set(RECORDINGS.rglob("*.wav")) if RECORDINGS.exists() else set()
    process, log, log_path = launch("wav-" + name, RUNTIME_CONFIG)
    command = None
    result = {"case": name, "log": str(log_path)}
    try:
        await wait_healthy(process, log_path)
        robot = Simulator("mock-fridge-01", "模拟办公区", ["前台", "充电点"], [], 0.5)
        command = asyncio.create_task(command_session(URL, secret("sim-robot-token"), robot, interval=1))
        async with httpx.AsyncClient(base_url="http://127.0.0.1:18764", trust_env=False, timeout=10,
                                    headers={"Authorization": "Bearer " + secret("user-token")}) as client:
            async with asyncio.timeout(15):
                while True:
                    if command.done():
                        await command
                        raise RuntimeError("Simulator command connection ended early")
                    robots = await get_json(client, "/api/robot-agent/robots")
                    connected = [r for r in robots if r["connected"]]
                    require(all(r["binding_id"] == "pcm-mock-fridge-binding" for r in connected),
                            "An unexpected robot connected to the test platform")
                    if connected and not connected[0]["issues"]:
                        break
                    await asyncio.sleep(0.25)
            upload_dir = output_root / name
            upload = await send_pcm(URL, secret("sim-robot-token"), "mock-fridge-01-voice", fixture,
                                    output_dir=upload_dir, timeout=150, expect_wake=True, expect_task=True)
            require(len(upload["segments"]) == len(upload["tasks"]) == 1, "Expected exactly one spoken task")
            result["upload"] = upload
            task_id = upload["tasks"][0]["task_id"]
            segment_id = upload["segments"][0]["segment_id"]
            result["task"] = await task_result(client, task_id)
            result["database"] = await evidence(task_id, segment_id)
            after = set(RECORDINGS.rglob("*.wav")) if RECORDINGS.exists() else set()
            new_files = after - before
            if name == "save":
                require(len(new_files) == 1, "Expected exactly one platform recording")
                saved = next(iter(new_files))
                require(saved.stem.endswith(segment_id), "Filename lacks segment association")
                require(saved.name.startswith(saved.parent.name.replace("-", "")), "Filename/date directory mismatch")
                require(saved.read_bytes() == (upload_dir / "segment-001.wav").read_bytes(), "Saved WAV differs from platform WAV")
                result["saved_wav"] = {"path": str(saved), "bytes": saved.stat().st_size,
                                       "sha256": sha256(saved.read_bytes()).hexdigest()}
            else:
                require(not new_files, "A platform recording was unexpectedly written")
            if name == "write-failure":
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                require("PCM WAV save failed" in log_text and segment_id in log_text, "Save error was not logged")
            result["status"] = "passed"
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            return result
    finally:
        if command is not None:
            command.cancel()
            await asyncio.gather(command, return_exceptions=True)
        try:
            if process.poll() is None:
                result["shutdown"] = await stop_with_ctrl_c(process, log_path)
        finally:
            log.close()
            blocker.unlink(missing_ok=True)


async def main(args):
    runtime = json.loads((TEST / "local-runtime.json").read_text(encoding="utf-8-sig"))
    base_config = json.loads((ROOT / "config/dev.local.json").read_text(encoding="utf-8-sig"))
    require(runtime["http"]["port"] == 18764 and runtime["postgresql"]["port"] == 55440,
            "This verification only targets the approved test ports")
    require(not listening(18764), "18764 is occupied; stop the identified test instance first")
    require(not APP_CONFIG.exists() and not RUNTIME_CONFIG.exists(), "Temporary configuration already exists")
    os.environ.update(load_provider_environment(args.provider_env_file, args.asr_credential))
    output_root = TEST / ("wav-e2e-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output_root.mkdir()
    report = {"status": "running", "test_platform": 18764, "test_database": 55440,
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_hashes": {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                  "src/robot_agent/application/pcm_segment_service.py", "src/robot_agent/deployment.py")},
              "input_wav": str(args.wav), "input_sha256": sha256(args.wav.read_bytes()).hexdigest(), "cases": []}
    try:
        for name in ("save", "without-config", "write-failure"):
            report["active_case"] = name
            report["cases"].append(await run_case(name, args.wav, output_root, base_config, runtime))
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        APP_CONFIG.unlink(missing_ok=True)
        RUNTIME_CONFIG.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-providers", action="store_true", required=True)
    parser.add_argument("--provider-env-file", type=Path, required=True)
    parser.add_argument("--asr-credential", required=True)
    parser.add_argument("--wav", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main(args))
