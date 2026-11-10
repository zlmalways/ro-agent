# Windows 本机测试平台启动

本入口使用第一步准备好的独立 PostgreSQL 数据目录、数据库和测试凭据。运行当前工作区源码，不切换正式平台。当前已验证 Windows Git Bash；其他系统和终端模拟器不在本次验证范围内。

## 日常启动与退出

在 Git Bash 中执行：

```bash
cd /d/codex_chat/2026-10-25/new-chat/ro-agent-mac-recovery
bash start-dev.sh
```

等终端出现 `Application startup complete.`，打开 `http://127.0.0.1:18764`。

按 `Ctrl+C` 正常关闭本次测试平台。平台端口释放，测试 PostgreSQL 继续运行，数据保留。不要把关闭终端窗口、强杀进程等同于已验证的 Ctrl+C 正常退出。

## 启动配置与实际顺序

主启动配置是项目内 `test-db/local-runtime.json`；平台业务配置是 `config/dev.local.json`。二者都是本机文件，已被 Git 忽略。

1. `start-dev.sh` 定位当前项目，用 PowerShell 只读取 JSON 中的 Python 程序路径，然后交给 `tools/dev_runtime.py`。本机无需把 Python 或 PostgreSQL 加入 PATH。
2. Python 读取同一份 JSON，检查项目路径、数据库目录、测试端口、平台配置及凭据文件。正式端口 `18763/55439`、项目外数据库目录和非测试数据库名会被拒绝。
3. 预留测试平台端口。已有平台占用端口时直接报错，不停止已有服务。
4. 用 `pg_ctl status` 核对配置指定的测试数据库目录。已运行则复用；未运行则通过 `pg_ctl start` 启动并等待，后台数据库脱离平台的控制台。此入口不会执行 `initdb`、创建账号或数据库、清空历史或再次复制正式配置。
5. 用测试管理员凭据只读核对真实数据库目录、端口及库所有者，再用测试应用账号验证连接。失败就退出，不启动平台。
6. 将测试应用连接信息和用户/机器人令牌放入当前进程环境；若 `config/dev.local.json` 开启 `allow_external_calls`，再从其中 `llm.credentials_file` 指向的本机 JSON 中只读取所选提供方的模型变量，设置 `RO_AGENT_CONFIG_FILE`，加载当前项目 `src/robot_agent/app.py`。模型密钥只在当前测试平台进程及其子进程环境中存在，不写入仓库或启动输出。
7. 现有平台启动代码自行检查并执行缺少的数据库迁移、登记机器人绑定、初始化业务组件和后台循环。完成后才提供 HTTP 服务。脚本没有复制业务逻辑。

`local-runtime.json` 中的关键内容：

| 字段 | 本机用途 |
| --- | --- |
| `python` | 运行平台的 Python 程序 |
| `http.host / port` | 当前为 `127.0.0.1:18764` |
| `postgresql.bin_directory` | 已有 PostgreSQL 程序目录 |
| `postgresql.data_directory` | 项目内 `test-db/pgdata` |
| `postgresql.host / port / database / user` | 当前为 `127.0.0.1:55440`，库名与应用账号均为 `ro_agent_sim_test` |
| `postgresql.password_file` | 测试应用账号密码文件 |
| `postgresql.admin_password_file` | 测试集群管理员密码文件，仅用于启动前只读核对，不交给平台 |
| `credentials` | 测试用户、两个机器人令牌文件的位置 |

要让本机测试平台使用真实规划模型，`config/dev.local.json` 需要设置 `allow_external_calls: true`，并配置 `llm.provider` 和 `llm.credentials_file`。凭据文件只需包含对应的 `DEEPLEAP_LLM_PROVIDER`、`<PROVIDER>_BASE_URL`、`<PROVIDER>_MODEL`、`<PROVIDER>_API_KEY` 及可选超时变量；启动器不会读取其中的正式数据库或机器人令牌。

密码和令牌不出现在启动命令或进度输出中。数据库日志见配置里的 `log_file`；控制命令日志写入同目录 `pg-control.log`。

## 实际验收

显式验收入口：`tests/acceptance/verify_dev_launcher.py`。它使用第一步本机验证辅助文件、真实 PostgreSQL、真实 `.sh`、当前平台源码、HTTP 和数据库查询，不使用业务替身。它不是普通 pytest 自动收集的测试。

```bash
"D:/ro-agent-data/runtime/pcm-platform-20261105/Scripts/python.exe" -B \
  tests/acceptance/verify_dev_launcher.py --allow-test-db-restart
```

`--allow-test-db-restart` 会在核对身份后，仅停止并重新启动本项目测试数据库，以验证冷启动；运行前应关闭自己正在使用的测试平台。验收结束后平台关闭、数据库继续运行。

验收覆盖：冷启动、已有数据库 PID 不变地复用、重复平台启动被拒绝、真实独立 Windows 控制台 Ctrl+C、平台正常关闭日志、测试库继续运行，以及错误密码/正式端口/项目外数据目录在平台启动前被拒绝。报告为 `test-db/launcher-verification.json`，各用例日志为 `test-db/launcher-*.log`。

当前业务配置关闭机器人执行和 PCM 语音处理；外部模型调用由 `config/dev.local.json` 的 `allow_external_calls` 和 `llm` 配置控制。用户和机器人配置可加载，两台机器人保持离线。启动验收不证明模型规划、语音识别或真机动作能力。
