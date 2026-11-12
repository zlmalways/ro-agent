"""Local test launcher; the application retains all migration/business logic."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "test-db"


class StartupError(Exception):
    """A safe, user-facing error that never includes credential values."""


def require(condition, message):
    if not condition:
        raise StartupError(message)


def announce(message):
    print(message, flush=True)


def project_path(value, *, parent=ROOT, must_exist=True):
    path = Path(value)
    require(path.is_absolute(), "配置中的路径必须是绝对路径。")
    path = path.resolve()
    require(path.is_relative_to(parent.resolve()), "配置中的写入或凭据路径不属于本项目测试目录。")
    require(not must_exist or path.exists(), "配置引用的本机文件或目录不存在，请先完成数据库准备。")
    return path


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise StartupError("无法读取本机 JSON 配置，请核对文件及权限。") from None
    require(isinstance(value, dict), "本机配置必须是 JSON 对象。")
    return value


def read_secret(value):
    path = project_path(value, parent=TEST_ROOT / "private")
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise StartupError("无法读取测试凭据文件，请核对当前用户的目录权限。") from None
    require(bool(secret), "测试凭据文件为空。")
    return secret


def read_external_settings(application):
    sys.path.insert(0, str(ROOT / "src"))
    from robot_agent.asr_config import load_asr_config, read_credentials

    if application.get("allow_external_calls") is not True:
        return {}, None
    configuration = application.get("llm")
    require(isinstance(configuration, dict), "已启用外部模型调用，但缺少本机 llm 配置。")
    provider = configuration.get("provider", "")
    require(provider in {"gpt", "deepseek", "relay"}, "llm.provider 必须是 gpt、deepseek 或 relay。")
    try:
        credentials = read_credentials(application)
        asr_config = load_asr_config(application, credentials=credentials)
    except ValueError as exc:
        raise StartupError(str(exc)) from None
    prefix = provider.upper()
    environment = {"DEEPLEAP_LLM_PROVIDER": provider}
    for suffix in ("BASE_URL", "API_KEY", "MODEL"):
        name = f"{prefix}_{suffix}"
        value = credentials.get(name, "")
        require(isinstance(value, str) and value.strip(),
                f"模型凭据文件缺少 {name}。")
        environment[name] = value.strip()
    timeout_name = f"{prefix}_TIMEOUT_S"
    timeout = credentials.get(timeout_name, "")
    if timeout not in (None, ""):
        require(isinstance(timeout, (int, float, str)) and str(timeout).strip(),
                f"模型凭据文件中的 {timeout_name} 无效。")
        environment[timeout_name] = str(timeout).strip()
    return environment, asr_config


def read_settings(config_path):
    config_path = project_path(str(config_path.resolve()), parent=TEST_ROOT)
    runtime = read_json(config_path)
    require(Path(runtime["project_directory"]).resolve() == ROOT, "启动配置指向其他项目目录。")
    require(Path(runtime["python"]).resolve() == Path(sys.executable).resolve(), "运行中的 Python 与配置不一致。")
    application_file = project_path(runtime["application_config"], parent=ROOT / "config")
    application = read_json(application_file)
    database = runtime["postgresql"]
    http = runtime["http"]
    require(database["host"] == http["host"] == "127.0.0.1", "本机测试入口只允许 127.0.0.1。")
    for port in (database["port"], http["port"]):
        require(type(port) is int and 1024 <= port <= 65535, "端口必须是 1024 到 65535 的整数。")
        require(port not in {18763, 55439}, "拒绝使用正式平台或正式数据库端口。")
    require(database["port"] != http["port"], "平台与数据库不能使用同一端口。")
    require(database["database"] == database["user"] == "ro_agent_sim_test", "仅允许本次独立测试数据库和账号。")
    data = project_path(database["data_directory"], parent=TEST_ROOT)
    require((data / "PG_VERSION").is_file(), "测试数据库尚未初始化；日常启动不会自动建库。")
    log = project_path(database["log_file"], parent=TEST_ROOT, must_exist=False)
    require(log.parent.is_dir(), "数据库日志目录不存在。")
    pg_ctl = Path(database["bin_directory"]).resolve() / "pg_ctl.exe"
    require(pg_ctl.is_file(), "找不到配置指定的 pg_ctl.exe。")
    require(application["database"]["dsn_env"] == "RO_AGENT_TEST_DATABASE_URL", "平台必须引用独立测试数据库环境变量。")
    require(application["database"].get("schema", "public") == "public", "当前测试库使用 public schema。")
    require(application["deployment"]["id"] == "local-sim-test", "平台配置不是本次测试部署。")
    project_path(application["deployment"]["lock_path"], parent=TEST_ROOT, must_exist=False)
    references = {row["token_env"] for row in application["users"] + application["robots"]}
    require(references == set(runtime["credentials"]), "用户/机器人凭据引用与本机配置不一致。")
    require(all(name.startswith("RO_AGENT_TEST_") for name in references), "只允许独立测试凭据环境变量。")
    secrets = {name: read_secret(path) for name, path in runtime["credentials"].items()}
    llm_environment, asr_config = read_external_settings(application)
    return {
        "runtime": runtime, "application": application, "application_file": application_file,
        "data": data, "log": log, "pg_ctl": pg_ctl, "secrets": secrets,
        "llm_environment": llm_environment,
        "asr_config": asr_config,
        "password": read_secret(database["password_file"]),
        "admin_password": read_secret(database["admin_password_file"]),
    }


def pg_control(settings, *arguments):
    # PostgreSQL must not share the platform terminal's Ctrl+C console.
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        # The background postmaster may inherit pipe handles on Windows. A file
        # lets us wait for pg_ctl itself without waiting for the database to exit.
        with settings["log"].with_name("pg-control.log").open("ab") as output:
            return subprocess.run(
                [str(settings["pg_ctl"]), *arguments, "-D", str(settings["data"])],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=flags, timeout=40, check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise StartupError("数据库进程检查/启动失败或超时，请检查 test-db 下的数据库日志。") from None


def reserve_http(settings):
    http = settings["runtime"]["http"]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((http["host"], http["port"]))
    except OSError:
        listener.close()
        raise StartupError(f"测试平台端口 {http['port']} 已被占用或不可用，未停止任何已有服务。") from None
    # Reserve now, but do not accept HTTP until database preparation succeeds.
    listener.setblocking(False)
    return listener


async def prepare_database(settings):
    import asyncpg

    database = settings["runtime"]["postgresql"]
    status = pg_control(settings, "status")
    if status.returncode == 3:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((database["host"], database["port"]))
        except OSError:
            raise StartupError("数据库端口已被其他进程占用，未尝试接管或停止它。") from None
        finally:
            probe.close()
        announce(f"[2/5] 启动测试数据库：127.0.0.1:{database['port']}，目录 {settings['data']}")
        result = pg_control(settings, "start", "-l", str(settings["log"]), "-w", "-t", "30",
                            "-o", f"-h 127.0.0.1 -p {database['port']}")
        require(result.returncode == 0, "测试数据库启动失败，请检查配置中的数据库日志；未启动平台。")
    elif status.returncode == 0:
        announce(f"[2/5] 复用测试数据库：127.0.0.1:{database['port']}")
    else:
        raise StartupError("无法确认指定测试数据库的状态；未启动平台。")

    # Administrator access is limited to identity checks; never passed to the app.
    for attempt in range(20):
        try:
            admin = await asyncpg.connect(host=database["host"], port=database["port"],
                                          user="postgres", database="postgres",
                                          password=settings["admin_password"], timeout=2)
            break
        except asyncpg.PostgresError:
            raise StartupError("测试数据库管理认证失败；未启动平台。") from None
        except (OSError, TimeoutError):
            if attempt == 19:
                raise StartupError("等待测试数据库连接超时；未启动平台。") from None
            await asyncio.sleep(0.25)
    try:
        actual = Path(await admin.fetchval("SHOW data_directory")).resolve()
        require(actual == settings["data"], "连接到的数据库目录与配置不符；未启动平台。")
        require(int(await admin.fetchval("SHOW port")) == database["port"], "数据库实际端口与配置不符。")
        owner = await admin.fetchval("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=$1", database["database"])
        require(owner == database["user"], "测试数据库不存在或所有者不符；日常启动不会创建数据库。")
    finally:
        await admin.close()
    try:
        connection = await asyncpg.connect(host=database["host"], port=database["port"],
                                           database=database["database"], user=database["user"],
                                           password=settings["password"], timeout=5)
    except (asyncpg.PostgresError, OSError, TimeoutError):
        raise StartupError("测试数据库应用账号连接失败，请检查测试密码和数据库状态；未启动平台。") from None
    try:
        actual = await connection.fetchrow("SELECT current_database(), current_user, inet_server_port()")
        require(tuple(actual) == (database["database"], database["user"], database["port"]), "数据库连接身份不符合配置。")
    finally:
        await connection.close()
    announce(f"[3/5] 数据库连接核对通过：{database['database']}；测试凭据已读取（不显示内容）。")


async def run(settings):
    import uvicorn

    listener = reserve_http(settings)
    try:
        await prepare_database(settings)
        database = settings["runtime"]["postgresql"]
        os.environ.update(settings["secrets"])
        os.environ.update(settings["llm_environment"])
        password = quote(settings["password"], safe="")
        os.environ["RO_AGENT_TEST_DATABASE_URL"] = (
            f"postgresql://{database['user']}:{password}@{database['host']}:{database['port']}/{database['database']}"
        )
        os.environ["RO_AGENT_CONFIG_FILE"] = str(settings["application_file"])
        sys.path.insert(0, str(ROOT / "src"))
        app_module = importlib.import_module("robot_agent.app")
        require(Path(app_module.__file__).resolve() == ROOT / "src/robot_agent/app.py", "加载的不是当前项目源码。")
        from robot_agent.deployment import build_app
        app = build_app(settings["application"], asr_config=settings["asr_config"])
        announce(f"[4/5] 启动当前项目平台，配置：{settings['application_file']}")
        asr_name = settings["asr_config"].provider if settings["asr_config"] else "disabled"
        announce(f"ASR JSON 配置已校验，供应商：{asr_name}；不显示密钥。")
        announce("[5/5] 平台将连接数据库、检查表结构并登记机器人；等待 Application startup complete。")
        http = settings["runtime"]["http"]
        announce(f"访问地址：http://{http['host']}:{http['port']}；Ctrl+C 停止平台，测试数据库保持运行。")
        server = uvicorn.Server(uvicorn.Config(
            app, host=http["host"], port=http["port"],
            ws="websockets-sansio", ws_max_size=settings["application"]["limits"]["max_frame_bytes"],
            timeout_graceful_shutdown=15, access_log=False, lifespan="on",
        ))
        try:
            await server.serve(sockets=[listener])
        finally:
            lifespan = getattr(server, "lifespan", None)
            require(not server.force_exit, "平台被再次中断，未确认正常关闭。")
            require(not getattr(lifespan, "error_occurred", False), "平台启动或关闭失败，请检查上方应用日志。")
            require(not getattr(lifespan, "shutdown_failed", False), "平台关闭失败，请检查上方应用日志。")
        require(server.started, "平台启动未完成，请检查上方应用日志。")
    finally:
        listener.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="启动本项目独立测试平台；Ctrl+C 只停止平台。")
    parser.add_argument("--config", type=Path, default=TEST_ROOT / "local-runtime.json")
    arguments = parser.parse_args()
    try:
        if os.name == "nt":
            # Git Bash can pass Windows' inheritable IGNORE_CTRL_C flag to a
            # native Python process. Uvicorn's Python handlers alone do not
            # clear it, so enable console delivery before preparing the DB.
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
            kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
            enabled = kernel.SetConsoleCtrlHandler(None, False)
            require(enabled or ctypes.get_last_error() == 6, "无法启用 Windows Ctrl+C 处理。")
        if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        settings = read_settings(arguments.config)
        announce(f"[1/5] 已读取本机启动配置：{arguments.config.resolve()}")
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        announce("已响应 Ctrl+C，退出测试平台启动入口；测试数据库保持运行。")
        return 0
    except StartupError as exc:
        print(f"启动失败：{exc}", file=sys.stderr, flush=True)
        return 1
    except (KeyError, TypeError, ValueError):
        print("启动失败：配置缺少必需字段或字段类型不正确。", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        # Dependency exceptions can embed DSNs; expose their type, not secrets.
        print(f"启动失败：{type(exc).__name__}；请检查本机配置、依赖及数据库日志。", file=sys.stderr, flush=True)
        return 1
    announce("测试平台已退出；测试数据库保持运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
