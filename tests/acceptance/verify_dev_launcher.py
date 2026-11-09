"""Opt-in Windows acceptance test of the real Bash launcher and Ctrl+C.

Run with the project's configured Python. This is not collected by pytest.
Only --allow-test-db-restart permits stopping the project's test database.
No platform/business dependency is replaced by a test double.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx


ROOT = Path(__file__).resolve().parents[2]
TEST = ROOT / "test-db"
START = ROOT / "start-dev.sh"
CONFIG = TEST / "local-runtime.json"
REPORT = TEST / "launcher-verification.json"
BASH = Path(r"C:\Users\14010\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\Programs\Git\bin\bash.exe")
PRIVATE = TEST / "private"
PGDATA = TEST / "pgdata"
PGCTL = Path(r"D:\ro-agent-data\runtime\pgsql\bin\pg_ctl.exe")
APP_PORT = 18764
DB_PORT = 55440


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def secret(name: str) -> str:
    return (PRIVATE / name).read_text(encoding="ascii").strip()


def listening(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.4)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def pg_pid() -> int:
    return int((PGDATA / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0])


def hidden_startup() -> subprocess.STARTUPINFO:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return startup


def launch(label: str, config: Path = CONFIG):
    log_path = TEST / f"launcher-{label}.log"
    log = log_path.open("wb")
    environment = dict(os.environ)
    environment.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen(
        [str(BASH), "--noprofile", "--norc", START.as_posix(), "--config", config.as_posix()],
        cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=hidden_startup(),
    )
    return process, log, log_path


def log_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def application_pid(log_path: Path) -> int:
    matches = re.findall(r"Started server process \[(\d+)\]", log_text(log_path))
    require(bool(matches), f"Uvicorn PID missing from {log_path.name}")
    return int(matches[-1])


def real_ctrl_c(pid: int) -> None:
    # Send only inside the hidden, isolated console created by launch().
    # CTRL_C_EVENT cannot target a nonzero process group: Microsoft documents
    # that GenerateConsoleCtrlEvent(0, pid) may succeed without delivery.
    code = r'''
import ctypes, sys, time
from ctypes import wintypes
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.AttachConsole.argtypes = [wintypes.DWORD]
k.AttachConsole.restype = wintypes.BOOL
k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
k.SetConsoleCtrlHandler.restype = wintypes.BOOL
k.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
k.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    if not k.SetConsoleCtrlHandler(None, True):
        raise ctypes.WinError(ctypes.get_last_error())
    if not k.GenerateConsoleCtrlEvent(0, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    time.sleep(0.3)
finally:
    k.FreeConsole()
'''
    subprocess.run([sys.executable, "-B", "-c", code, str(pid)], check=True,
                   capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)


async def wait_healthy(process, log_path: Path) -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            require(process.poll() is None, f"Launcher exited early ({process.returncode}); see {log_path.name}")
            try:
                response = await client.get(f"http://127.0.0.1:{APP_PORT}/health")
                if response.status_code == 200 and response.json().get("configured") is True:
                    application_pid(log_path)
                    return
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.15)
    raise RuntimeError(f"Platform did not become healthy; see {log_path.name}")


async def http_checks(app_config) -> dict:
    outputs = {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{APP_PORT}", trust_env=False, timeout=5) as client:
        health = await client.get("/health")
        require(health.status_code == 200 and health.json() == {"configured": True, "execution_enabled": False}, "Health mismatch")
        outputs["health"] = {"status": health.status_code, "body": health.json()}
        unauth = await client.get("/api/robot-agent/robots")
        require(unauth.status_code == 401, "Unauthenticated access was accepted")
        outputs["unauthenticated_robots_status"] = unauth.status_code
        headers = {"Authorization": "Bearer " + secret("user-token")}
        for name in ("session", "robots", "tasks"):
            response = await client.get(f"/api/robot-agent/{name}", headers=headers)
            require(response.status_code == 200, f"{name} HTTP status mismatch")
            outputs[name] = {"status": response.status_code, "body": response.json()}
        require(outputs["session"]["body"] == {"actor_id": app_config["users"][0]["actor_id"]}, "Session identity mismatch")
        robots = outputs["robots"]["body"]
        require({r["binding_id"] for r in robots} == {r["binding_id"] for r in app_config["robots"]}, "Robot identity mismatch")
        require(all(not r["connected"] and "OFFLINE" in r["issues"] for r in robots), "Unexpected robot connection")
        require(outputs["tasks"]["body"]["tasks"] == [], "Task history is not empty")
    return outputs


async def stop_with_ctrl_c(process, log_path: Path) -> dict:
    pid = application_pid(log_path)
    real_ctrl_c(pid)
    deadline = time.monotonic() + 20
    while process.poll() is None and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    require(process.poll() is not None, f"Ctrl+C did not close launcher PID {process.pid}")
    text = log_text(log_path)
    require("Application shutdown complete." in text, "Application did not finish normal shutdown")
    require(not listening(APP_PORT), "Test platform port remains open after Ctrl+C")
    require(listening(DB_PORT), "Test database stopped together with platform")
    require(process.returncode in (0, 130, -1073741510, 3221225786), f"Unexpected Ctrl+C exit code {process.returncode}")
    return {"method": "isolated Windows console CTRL_C_EVENT", "application_pid": pid,
            "launcher_exit_code": process.returncode, "graceful_shutdown_log": True,
            "http_port_closed": True, "postgresql_still_running": True}


async def run_case(label: str, app_config, snapshot, *, expected_pg_pid: int | None, duplicate: bool) -> dict:
    require(not listening(APP_PORT), "18764 is occupied; refusing to disturb its owner")
    process, log, log_path = launch(label)
    try:
        await wait_healthy(process, log_path)
        current_pg_pid = pg_pid()
        if expected_pg_pid is not None:
            require(current_pg_pid == expected_pg_pid, "Running test database was restarted instead of reused")
        result = {"case": label, "postgresql_pid": current_pg_pid,
                  "http": await http_checks(app_config), "database": await snapshot(app_config),
                  "log": str(log_path)}
        if duplicate:
            second, second_log, second_path = launch(label + "-duplicate")
            try:
                deadline = time.monotonic() + 20
                while second.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                require(second.poll() is not None and second.returncode != 0, "Duplicate launcher did not reject occupied port")
                require(process.poll() is None and pg_pid() == current_pg_pid, "Duplicate launcher disturbed running services")
                await http_checks(app_config)
                result["duplicate_rejected"] = {"exit_code": second.returncode, "first_platform_healthy": True, "log": str(second_path)}
            finally:
                second_log.close()
        result["ctrl_c"] = await stop_with_ctrl_c(process, log_path)
        require(pg_pid() == current_pg_pid, "Ctrl+C changed the database process")
        return result
    finally:
        if process.poll() is None:
            # Never turn a failed graceful-exit check into a passing forced kill.
            try:
                real_ctrl_c(application_pid(log_path))
            except Exception:
                pass
        log.close()


async def rejection_checks(runtime: dict) -> list[dict]:
    """Real launches with invalid input; no business handlers are replaced."""
    import copy
    from contextlib import ExitStack
    import tempfile

    require(not listening(APP_PORT), "Refusal checks need a free test platform port")
    before_pid = pg_pid()
    results = []
    # Register cleanup before writing so partial writes and launch failures are
    # covered too. These inputs are disposable; logs/reports remain evidence.
    with ExitStack() as temporary_files:
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=PRIVATE,
                                         prefix="launcher-invalid-", delete=False) as password_file:
            password_path = Path(password_file.name)
            temporary_files.callback(password_path.unlink, missing_ok=True)
            password_file.write("not-the-real-test-password")
        cases = []
        wrong_password = copy.deepcopy(runtime)
        wrong_password["postgresql"]["password_file"] = str(password_path)
        cases.append(("wrong-password", wrong_password, "应用账号连接失败"))
        formal_port = copy.deepcopy(runtime)
        formal_port["postgresql"]["port"] = 55439
        cases.append(("formal-port", formal_port, "拒绝使用正式平台"))
        outside_data = copy.deepcopy(runtime)
        outside_data["postgresql"]["data_directory"] = str(ROOT.parent / "outside-test-data")
        cases.append(("outside-data-directory", outside_data, "不属于本项目测试目录"))
        for label, config, expected in cases:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=TEST,
                                             prefix=f"launcher-input-{label}-", suffix=".json",
                                             delete=False) as config_file:
                config_path = Path(config_file.name)
                temporary_files.callback(config_path.unlink, missing_ok=True)
                json.dump(config, config_file, ensure_ascii=False, indent=2)
            process, log, log_path = launch(label, config_path)
            try:
                deadline = time.monotonic() + 20
                while process.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                require(process.poll() is not None and process.returncode != 0,
                        f"Invalid configuration was not rejected: {label}")
                output = log_text(log_path)
                require(expected in output and "Started server process" not in output,
                        f"Wrong refusal or application started: {label}")
                require(not listening(APP_PORT) and pg_pid() == before_pid,
                        f"Invalid configuration disturbed test services: {label}")
                require("not-the-real-test-password" not in output, "Credential leaked into launcher log")
                results.append({"case": label, "exit_code": process.returncode,
                                "application_not_started": True, "database_pid_unchanged": True,
                                "log": str(log_path)})
            finally:
                log.close()
                config_path.unlink(missing_ok=True)
    return results


async def run(allow_restart: bool) -> dict:
    require(os.name == "nt", "This acceptance harness is for Windows console Ctrl+C")
    require(START.is_file(), f"Missing launcher: {START}")
    require(BASH.is_file(), f"Missing configured Git Bash: {BASH}")
    runtime = json.loads(CONFIG.read_text(encoding="utf-8"))
    require(Path(runtime["project_directory"]).resolve() == ROOT.resolve(), "Unexpected project directory")
    require(Path(runtime["postgresql"]["data_directory"]).resolve() == PGDATA.resolve(), "Refusing unexpected data directory")
    require(runtime["postgresql"]["port"] == DB_PORT and runtime["http"]["port"] == APP_PORT, "Unexpected test ports")
    require(runtime["postgresql"]["database"] == "ro_agent_sim_test", "Unexpected database name")
    require(not listening(APP_PORT), "18764 is occupied; stop only your test platform before this acceptance run")
    spec = importlib.util.spec_from_file_location("step1_verification", TEST / "verify_step1.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    preflight = await helper.database_preflight()
    app_config = json.loads(Path(runtime["application_config"]).read_text(encoding="utf-8"))
    require(app_config["deployment"]["execution_enabled"] is False, "Acceptance requires robot execution disabled")
    require(app_config["allow_external_calls"] is False, "Acceptance requires external model calls disabled")
    require(all(value is False for value in app_config["voice_pcm"].values()), "Acceptance requires PCM processing disabled")
    baseline = await helper.snapshot(app_config)
    first_pid = pg_pid()
    cases = []
    if allow_restart:
        # Preflight has just proved the server's actual data directory and port.
        subprocess.run([str(PGCTL), "stop", "-D", str(PGDATA), "-m", "fast", "-w", "-t", "20"],
                       check=True, capture_output=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
        require(not listening(DB_PORT), "Test database did not stop for the cold-start case")
        cases.append(await run_case("cold-start", app_config, helper.snapshot, expected_pg_pid=None, duplicate=False))
        first_pid = pg_pid()
    cases.append(await run_case("reuse-running-db", app_config, helper.snapshot, expected_pg_pid=first_pid, duplicate=True))
    rejections = await rejection_checks(runtime)
    final = await helper.snapshot(app_config)
    require(final == baseline, "Database contents changed during launcher acceptance")
    require(all(case["database"] == baseline for case in cases), "A launcher round changed database contents")
    return {"status": "passed", "verified_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "launcher": str(START), "runtime_config": str(CONFIG), "postgresql_preflight": preflight,
            "cold_start_verified": allow_restart, "cases": cases, "rejections": rejections,
            "database_unchanged": True,
            "test_platform_stopped": True, "postgresql_still_running": True,
            "not_verified": ["model planning", "ASR/PCM", "physical robot actions", "manual keyboard in every terminal emulator"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-test-db-restart", action="store_true",
                        help="Permit stopping only the project's verified test PG instance for cold-start validation")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.allow_test_db_restart))
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(REPORT), **({"error": result["error"]} if "error" in result else {})}, ensure_ascii=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
