"""Opt-in real startup/HTTP/DB/Ctrl+C check; does not submit tasks or prove robot acceptance."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime

import httpx

from tests.acceptance.verify_dev_launcher import (
    BASH, ROOT, TEST, application_pid, hidden_startup, listening,
    pg_pid, real_ctrl_c, require, stop_with_ctrl_c, wait_healthy,
)


def active_test_process():
    if not listening(18764):
        return None
    command = ("Get-NetTCPConnection -State Listen -LocalPort 18764 -ErrorAction SilentlyContinue | "
               "ForEach-Object { Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess) } | "
               "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                            capture_output=True, text=True, check=True, timeout=15)
    if not result.stdout.strip():
        return None
    process = json.loads(result.stdout)
    require(isinstance(process, dict), "18764 has multiple listeners; stop and inspect")
    command_line = process["CommandLine"].lower().replace("\\", "/")
    require(ROOT.as_posix().lower() + "/tools/dev_runtime.py" in command_line
            and (TEST / "local-runtime.json").as_posix().lower() in command_line,
            "18764 is not the expected test launcher; refusing to stop it")
    return process["ProcessId"]


async def verify(allow_restart):
    require(os.name == "nt", "Windows console check only")
    existing = active_test_process()
    before_pg = pg_pid()
    formal_listening = listening(18763)
    log_folder = TEST / ("TEST-shared-launcher-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    log_folder.mkdir()
    log_directory = Path(json.loads((TEST / "local-runtime.json").read_text(encoding="utf-8"))["log_directory"])
    prior_logs = set(log_directory.glob("TEST-ro-agent-*.log")) if log_directory.exists() else set()
    handles = []
    processes = []
    passed = False

    def launch(label):
        log_path = log_folder / f"TEST-{label}.log"
        output = log_path.open("wb")
        output.write(f"TEST ONLY: shared launcher verification: {label}\n".encode())
        output.flush()
        handles.append(output)
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen([str(BASH), "--noprofile", "--norc", str(ROOT / "start-dev.sh")],
                                   cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=hidden_startup())
        processes.append((process, log_path))
        return process, log_path

    try:
        if existing:
            require(allow_restart, "Use --restart-running-test-platform only with approval")
            real_ctrl_c(existing)
            for _ in range(200):
                if not listening(18764):
                    break
                await asyncio.sleep(0.1)
            require(not listening(18764), "Existing test platform did not stop")
        process, log_path = launch("normal-start")
        await wait_healthy(process, log_path)
        require(pg_pid() == before_pg, "Test PostgreSQL was unexpectedly restarted")
        token = (TEST / "private/user-token").read_text(encoding="utf-8").strip()
        async with httpx.AsyncClient(base_url="http://127.0.0.1:18764", trust_env=False, timeout=5) as client:
            health = await client.get("/health")
            page = await client.get("/")
            denied = await client.get("/api/robot-agent/robots")
            require(health.status_code == page.status_code == 200, "Health/page failed")
            require(health.json()["configured"] is True and denied.status_code == 401, "Readiness/auth failed")
            for endpoint in ("session", "robots", "tasks"):
                response = await client.get("/api/robot-agent/" + endpoint,
                                            headers={"Authorization": "Bearer " + token})
                require(response.status_code == 200, f"Authenticated {endpoint} failed")
        duplicate, duplicate_log = launch("duplicate-start")
        await asyncio.to_thread(duplicate.wait, 30)
        require(duplicate.returncode != 0 and "已被占用" in duplicate_log.read_text(encoding="utf-8"),
                "Duplicate startup did not reject the occupied port")
        require(process.poll() is None and pg_pid() == before_pg, "Duplicate disturbed running service")
        result = await stop_with_ctrl_c(process, log_path)
        require(listening(18763) == formal_listening, "Formal listener changed during check")
        passed = True
        print(json.dumps({"status": "PASS", "health_page_authenticated_queries": True,
                          "duplicate_rejected": True, "test_database_pid_unchanged": True,
                          "ctrl_c": result, "formal_service_not_operated": True,
                          "not_verified": ["formal application startup", "model and real robot task"]},
                         ensure_ascii=False))
    finally:
        for process, path in processes:
            if process.poll() is None:
                try:
                    real_ctrl_c(application_pid(path))
                    await asyncio.to_thread(process.wait, 20)
                except Exception:
                    print(f"TEST process {process.pid} needs inspection; no force kill performed.")
        for handle in handles:
            handle.close()
        if passed and all(process.poll() is not None for process, _ in processes):
            owned_shell_logs = set()
            for _, path in processes:
                owned_shell_logs.update(re.findall(r"TEST-ro-agent-\d{8}-\d{6}-\d+\.log",
                                                   path.read_text(encoding="utf-8")))
            # Delete only this run's named logs; leave all pre-existing logs/data.
            for path in log_folder.iterdir():
                require(path.name.startswith("TEST-") and path.is_file(), "Unexpected test output")
                path.unlink()
            log_folder.rmdir()
            # Match only filenames printed by our own launcher processes; another
            # terminal may have started its own platform during this check.
            for name in owned_shell_logs:
                path = log_directory / name
                if path not in prior_logs:
                    path.unlink(missing_ok=True)
            print("TEST temporary logs deleted; no temporary configuration was created.")
        else:
            print(f"TEST logs retained for failed-check diagnosis: {log_folder}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart-running-test-platform", action="store_true")
    asyncio.run(verify(parser.parse_args().restart_running_test_platform))
