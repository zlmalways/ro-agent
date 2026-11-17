"""Shared test/formal launcher; migrations and business rules stay in the app."""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tomllib
from urllib.parse import quote, unquote, urlsplit

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
    require(path.is_relative_to(parent.resolve()), "配置路径不属于指定目录。")
    require(not must_exist or path.exists(), f"配置引用的本机文件或目录不存在：{path}")
    return path


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise StartupError(f"无法读取本机 JSON 配置，请核对文件及权限：{path}") from None
    require(isinstance(value, dict), "本机配置必须是 JSON 对象。")
    return value


def read_secret(value):
    path = Path(value)
    require(path.is_absolute(), "凭据文件必须使用绝对路径。")
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise StartupError(f"无法读取凭据文件，请核对路径及权限：{path}") from None
    require(bool(secret), "凭据文件为空。")
    return secret


def read_external_settings(application, credentials=None):
    sys.path.insert(0, str(ROOT / "src"))
    from robot_agent.asr_config import load_asr_config, read_credentials

    if application.get("allow_external_calls") is not True:
        return {}, None
    configuration = application.get("llm")
    require(isinstance(configuration, dict), "已启用外部模型调用，但缺少本机 llm 配置。")
    provider = configuration.get("provider", "")
    require(provider in {"gpt", "deepseek", "relay"}, "llm.provider 必须是 gpt、deepseek 或 relay。")
    try:
        credentials = read_credentials(application) if credentials is None else credentials
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
    from robot_agent.config import load_provider_config
    try:
        load_provider_config(environment)
    except ValueError:
        raise StartupError("规划模型配置无效，请核对所选供应商字段。") from None
    return environment, asr_config


def apply_overrides(base, overrides, prefix="application"):
    """Environment values replace existing fields; schema changes fail visibly."""
    require(isinstance(base, dict) and isinstance(overrides, dict), "环境覆盖配置必须是 JSON 对象。")
    result = deepcopy(base)
    for key, value in overrides.items():
        name = f"{prefix}.{key}"
        require(key in base, f"环境配置引用的字段不存在：{name}；请同步更新配置引用。")
        if isinstance(value, dict):
            require(isinstance(base[key], dict), f"环境配置字段结构已改变：{name}。")
            result[key] = apply_overrides(base[key], value, name)
        else:
            require(type(value) is type(base[key]), f"环境配置字段类型不一致：{name}。")
            result[key] = deepcopy(value)
    return result


def check_dependencies(application):
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    require(".".join(map(str, sys.version_info[:3])) in SpecifierSet(project["requires-python"]),
            "当前 Python 版本不符合项目要求。")
    requirements = list(project.get("dependencies", []))
    if application.get("voice_pcm", {}).get("enabled"):
        requirements += project["optional-dependencies"]["voice-pcm"]
    for specification in requirements:
        item = Requirement(specification)
        if item.marker and not item.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(item.name)
        except importlib.metadata.PackageNotFoundError:
            raise StartupError(f"现有 Python 缺少依赖：{item.name}；未自动安装。") from None
        require(installed in item.specifier, f"现有依赖版本不符合要求：{item.name}；未自动安装。")


def verify_release():
    manifest_file = ROOT / "source-manifest.json"
    if not manifest_file.exists():
        return
    manifest = read_json(manifest_file)
    require(bool(manifest), "发布文件清单为空。")
    for name, expected in manifest.items():
        path = project_path(str(ROOT / name))
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                f"发布文件已改变或缺失：{name}。")


def read_settings(config_path, environment=None):
    config_path = project_path(str(config_path.resolve()))
    runtime = read_json(config_path)
    mode = runtime["environment"]
    require(runtime.get("version") == 1 and mode in {"test", "formal"}, "启动配置版本或环境名称无效。")
    require(environment is None or mode == environment, "SH 入口与配置环境不一致，拒绝启动。")
    if runtime.get("project_directory"):
        require(Path(runtime["project_directory"]).resolve() == ROOT, "启动配置指向其他项目目录。")
    require(Path(runtime["python"]).resolve() == Path(sys.executable).resolve(), "运行中的 Python 与配置不一致。")
    application_file = Path(runtime["application_config"])
    if not application_file.is_absolute():
        application_file = config_path.parent / application_file
    application_file = project_path(str(application_file.resolve()))
    application = apply_overrides(read_json(application_file), runtime.get("application_overrides", {}))
    database = runtime["postgresql"]
    http = runtime["http"]
    require(database["host"] == "127.0.0.1", "数据库入口只允许本机 127.0.0.1。")
    for port in (database["port"], http["port"]):
        require(type(port) is int and 1024 <= port <= 65535, "端口必须是 1024 到 65535 的整数。")
    require(database["port"] != http["port"], "平台与数据库不能使用同一端口。")
    if mode == "test":
        require(http["host"] == "127.0.0.1", "测试平台只允许 127.0.0.1。")
        require(not {database["port"], http["port"]} & {18763, 55439}, "拒绝在测试入口使用正式端口。")
        require(database["database"] == database["user"] == "ro_agent_sim_test", "测试库身份不符。")
        require(application["database"]["dsn_env"] == "RO_AGENT_TEST_DATABASE_URL", "测试库凭据引用不符。")
        require(application["deployment"]["id"] == "local-sim-test", "测试部署身份不符。")
        data = project_path(database["data_directory"], parent=TEST_ROOT)
        project_path(application["deployment"]["lock_path"], parent=TEST_ROOT, must_exist=False)
    else:
        require((http["host"], http["port"], database["port"]) == ("0.0.0.0", 18763, 55439),
                "正式平台或数据库端口与本机正式约定不符。")
        require(database["database"] == database["user"] == "ro_agent_formal", "正式库身份不符。")
        require(application["database"]["dsn_env"] == "RO_AGENT_DATABASE_URL", "正式库凭据引用不符。")
        require(application["deployment"]["id"] == "local-formal-astribot", "正式部署身份不符。")
        data = Path(database["data_directory"]).resolve()
        require(data == Path("D:/ro-agent-data/data/pgdata").resolve(), "正式数据库目录不符。")
        for action in ("move_to", "vla_skill"):
            require(application["policy"]["actions"]["local-development"][action] == "require_confirmation",
                    "正式动作确认政策发生变化；本次发布不允许自动放宽。")
    require((data / "PG_VERSION").is_file(), "数据库尚未初始化；日常启动不会自动建库。")
    log = Path(database["log_file"])
    require(log.is_absolute(), "数据库日志必须使用绝对路径。")
    if mode == "test":
        project_path(str(log), parent=TEST_ROOT, must_exist=False)
    require(log.parent.is_dir(), "数据库日志目录不存在。")
    pg_ctl = Path(database["bin_directory"]).resolve() / "pg_ctl.exe"
    require(pg_ctl.is_file(), "找不到配置指定的 pg_ctl.exe。")
    require(application["database"].get("schema", "public") == "public", "当前数据库使用 public schema。")
    lock = Path(application["deployment"]["lock_path"])
    require(lock.is_absolute() and lock.parent.is_dir(), "部署锁文件目录不存在或不是绝对路径。")
    references = {row["token_env"] for row in application["users"] + application["robots"]}
    token_files = runtime.get("credentials", {})
    require(set(token_files) <= references, "令牌文件包含未引用的身份字段。")
    if mode == "test":
        require(references == set(token_files) and all(name.startswith("RO_AGENT_TEST_") for name in references),
                "测试身份必须全部使用独立测试令牌。")
        for value in [*token_files.values(), database["password_file"], database["admin_password_file"]]:
            project_path(value, parent=TEST_ROOT / "private")
    credentials = read_json(Path(application["credentials_file"])) if application.get("credentials_file") else {}
    secrets = {name: read_secret(token_files[name]) if name in token_files else credentials.get(name)
               for name in references}
    require(all(isinstance(value, str) and value.strip() for value in secrets.values()), "用户或机器人令牌缺失。")
    if database.get("password_file"):
        password = read_secret(database["password_file"])
    else:
        target = urlsplit(credentials.get(application["database"]["dsn_env"], ""))
        require(target.scheme in {"postgres", "postgresql"} and not target.query and not target.fragment,
                "数据库连接凭据格式无效。")
        require((target.hostname, target.port, unquote(target.path.lstrip('/')), unquote(target.username or ''))
                == (database["host"], database["port"], database["database"], database["user"]),
                "数据库凭据与环境配置身份不一致。")
        password = unquote(target.password or "")
        require(bool(password), "数据库密码缺失。")
    require(isinstance(database["admin_user"], str) and database["admin_user"], "数据库管理员账号缺失。")
    llm_environment, asr_config = read_external_settings(application, credentials)
    check_dependencies(application)
    return {
        "runtime": runtime, "application": application, "application_file": application_file,
        "data": data, "log": log, "pg_ctl": pg_ctl, "secrets": secrets,
        "llm_environment": llm_environment,
        "asr_config": asr_config,
        "password": password,
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
        raise StartupError("数据库进程检查/启动失败或超时，请检查配置中的数据库日志。") from None


def reserve_http(settings):
    http = settings["runtime"]["http"]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((http["host"], http["port"]))
    except OSError:
        listener.close()
        raise StartupError(f"平台端口 {http['port']} 已被占用或不可用，未停止任何已有服务。") from None
    # Reserve now, but do not accept HTTP until database preparation succeeds.
    listener.setblocking(False)
    return listener


async def prepare_database(settings):
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
        announce(f"[2/5] 启动数据库：127.0.0.1:{database['port']}，目录 {settings['data']}")
        result = pg_control(settings, "start", "-l", str(settings["log"]), "-w", "-t", "30",
                            "-o", f"-h 127.0.0.1 -p {database['port']}")
        require(result.returncode == 0, "数据库启动失败，请检查配置中的数据库日志；未启动平台。")
    elif status.returncode == 0:
        announce(f"[2/5] 复用数据库：127.0.0.1:{database['port']}")
    else:
        raise StartupError("无法确认指定数据库的状态；未启动平台。")
    await check_database(settings)


async def check_database(settings):
    """Read-only identity/migration checks. Never starts a server or runs SQL migrations."""
    import asyncpg

    database = settings["runtime"]["postgresql"]

    # Administrator access is limited to identity checks; never passed to the app.
    for attempt in range(20):
        try:
            admin = await asyncpg.connect(host=database["host"], port=database["port"],
                                          user=database["admin_user"], database="postgres",
                                          password=settings["admin_password"], timeout=2,
                                          server_settings={"default_transaction_read_only": "on"})
            break
        except asyncpg.PostgresError:
            raise StartupError("数据库管理认证失败；未启动平台。") from None
        except (OSError, TimeoutError):
            if attempt == 19:
                raise StartupError("等待数据库连接超时；未启动平台。") from None
            await asyncio.sleep(0.25)
    try:
        actual = Path(await admin.fetchval("SHOW data_directory")).resolve()
        require(actual == settings["data"], "连接到的数据库目录与配置不符；未启动平台。")
        require(int(await admin.fetchval("SHOW port")) == database["port"], "数据库实际端口与配置不符。")
        owner = await admin.fetchval("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=$1", database["database"])
        require(owner == database["user"], "数据库不存在或所有者不符；日常启动不会创建数据库。")
    finally:
        await admin.close()
    try:
        connection = await asyncpg.connect(host=database["host"], port=database["port"],
                                           database=database["database"], user=database["user"],
                                           password=settings["password"], timeout=5,
                                           server_settings={"default_transaction_read_only": "on"})
    except (asyncpg.PostgresError, OSError, TimeoutError):
        raise StartupError("数据库应用账号连接失败，请检查密码和数据库状态；未启动平台。") from None
    try:
        actual = await connection.fetchrow("SELECT current_database(), current_user, inet_server_port()")
        require(tuple(actual) == (database["database"], database["user"], database["port"]), "数据库连接身份不符合配置。")
        migrations = {}
        for path in sorted((ROOT / "db/migrations").glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            require(version not in migrations, "数据库迁移编号重复。")
            migrations[version] = (path.name, hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest())
        installed = {}
        if await connection.fetchval("SELECT to_regclass('public.ra_schema_migrations') IS NOT NULL"):
            installed = dict(await connection.fetch("SELECT version, checksum FROM public.ra_schema_migrations"))
        for version, checksum in installed.items():
            require(version in migrations and migrations[version][1] == checksum,
                    f"已执行数据库迁移 {version} 缺失或校验不符；拒绝启动。")
        pending = [name for version, (name, _) in migrations.items() if version not in installed]
        announce("待应用启动时执行的迁移：" + ("、".join(pending) or "无"))
    finally:
        await connection.close()
    announce(f"[3/5] 数据库只读核对通过：{database['database']}；凭据不显示。")


async def run(settings):
    import uvicorn

    listener = reserve_http(settings)
    try:
        await prepare_database(settings)
        database = settings["runtime"]["postgresql"]
        os.environ.update(settings["secrets"])
        os.environ.update(settings["llm_environment"])
        password = quote(settings["password"], safe="")
        os.environ[settings["application"]["database"]["dsn_env"]] = (
            f"postgresql://{database['user']}:{password}@{database['host']}:{database['port']}/{database['database']}"
        )
        # Pass the merged object directly; an app factory reading the base JSON
        # again would lose the selected environment overrides.
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
        announce(f"访问地址：http://{http['host']}:{http['port']}；Ctrl+C 停止平台，数据库保持运行。")
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
    parser = argparse.ArgumentParser(description="测试/正式共用启动程序；Ctrl+C 只停止平台。")
    parser.add_argument("--config", type=Path, default=TEST_ROOT / "local-runtime.json")
    parser.add_argument("--environment", choices=("test", "formal"), required=True)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", action="store_true", help="只校验文件、凭据引用及现有依赖")
    checks.add_argument("--check-db", action="store_true", help="额外只读核对已运行数据库；不启动或迁移")
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
        verify_release()
        settings = read_settings(arguments.config, arguments.environment)
        announce(f"[1/5] 已读取本机启动配置：{arguments.config.resolve()}")
        announce(f"环境：{arguments.environment}；代码目录：{ROOT}")
        if arguments.check or arguments.check_db:
            if arguments.check_db:
                asyncio.run(check_database(settings))
            announce("CHECK PASS：配置及现有依赖校验通过；未启动平台、未启停数据库、未执行迁移。")
            return 0
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        announce("已响应 Ctrl+C，退出平台启动入口；数据库保持运行。")
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
    announce("平台已退出；数据库保持运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
