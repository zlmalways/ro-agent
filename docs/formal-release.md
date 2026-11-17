# 本机测试与正式发布

两个入口随代码一起维护：`start-dev.sh` 启动测试平台，`start-formal.sh` 启动正式平台；它们调用同一份 `tools/start-runtime.sh` 和 `tools/dev_runtime.py`。

## 配置放在哪里

| 内容 | 当前文件 | 发布处理 |
| --- | --- | --- |
| 共同业务配置，包括规划供应商、ASR 和限额 | `config/dev.local.json` | 按当前工作目录原样复制 |
| 测试端口、数据库、凭据文件位置 | `test-db/local-runtime.json` | 不复制本机测试运行数据；提供 `config/dev-runtime.example.json` 模板 |
| 正式端口、数据库及业务字段覆盖 | `config/formal-runtime.local.json` | 随发布原样复制；模板为 `config/formal-runtime.example.json` |
| 密码、模型密钥、身份令牌 | 配置引用的项目外私有文件；测试令牌在 `test-db/private` | 不复制、不输出 |

本机的 `.local.json` 不提交 Git。模板不含密码值；在另一台电脑使用时，必须修改其中的本机路径。共同业务配置样例为 `config/application.example.json`，它不会覆盖当前本机配置。

正式环境用 `application_overrides` 明确覆盖数据库环境变量、部署身份/锁/主机、正式用户和机器人令牌引用、正式动作政策、外部调用开关、密钥文件位置、唤醒检查和录音路径。ASR、规划供应商和其他未覆盖字段随共同业务配置更新。覆盖只允许已有字段，字段被移动、改名或改变类型时会停止并报出字段名，不能静默使用旧配置。

`application_config` 指向业务 JSON；相对路径以环境配置所在目录为准。若移动业务 JSON，同时更新两个环境配置的这个引用。发布脚本读取引用，不再另写 ASR 字段迁移或修改 Python 源码。正式运行读取的是发布目录里的业务 JSON，不依赖 v1 工作目录里的文件。

## 每次准备正式发布

在 Git Bash 执行：

```bash
bash /d/ro-agent-data/releases/prepare-formal-release.sh --check
bash /d/ro-agent-data/releases/prepare-formal-release.sh
```

第二条读取当前 `feat/robot-agent-v1` 工作目录，包含未提交内容，复制 Git 未忽略的项目文件，加上明确选中的两份本机配置。`.git`、测试数据、数据库目录、密钥文件和 Python 环境不复制。脚本核对旧发布清单、不可改写的迁移、现有依赖和新副本，生成 `source-manifest.json`、`release-receipt.json`，最后把本次版本的具体路径写入固定的 `D:/ro-agent-data/releases/start-formal.sh`。固定入口经过 Bash 语法检查和 `--check` 实际转发检查后才替换，参数原样传给该版本的启动程序。

`D:/ro-agent-data/releases/current-formal.txt` 继续供发布基线核对和模拟器查找版本使用；固定启动入口不读取它，也不在启动时搜索版本。它只记录已准备的目录，不表示该版本正在运行。

发布脚本源码在项目的 `tools/prepare-formal-release.sh` 与 `tools/prepare_formal_release.py`。release 文件夹中的 SH 负责调用当前 v1 的实现；新版本的启动代码和配置则全部在发布目录中。

脚本不启停服务、不迁移数据库、不安装依赖、不备份或删除旧发布目录。复制或校验失败时不替换固定入口；固定入口写入失败时恢复原发布记录，并清理本次未完成的副本。若恢复本身也受系统错误阻止，会保留新副本并报错。源码在复制过程中发生变化会停止，请重新准备。

## 启动前会发生什么

由使用者停止旧平台，再执行固定命令。每次发布时，准备脚本已经将该文件里的目标路径改成新版本：

```bash
bash /d/ro-agent-data/releases/start-formal.sh
```

1. SH 选择环境配置，找到已有 Python；普通启动把控制台输出写入该环境的日志目录。
2. 共用 Python 校验发布文件、配置、密钥引用、既有依赖，以及测试/正式身份和端口隔离。
3. 预留平台端口；端口被占用时退出，不停止已有服务。
4. PostgreSQL 已运行就复用；没有运行则按配置启动。不会新建数据库、账号或清空记录。
5. 用管理员只读检查数据库目录、端口、所有者；用应用账号检查连接身份和已执行迁移的校验值。
6. 合并业务配置与环境覆盖，将必要凭据只放入进程内，加载该目录的应用代码。
7. 应用执行缺少的迁移、登记绑定并启动业务循环。看到 `Application startup complete.` 才表示平台启动完成。

按 Ctrl+C 只关闭本次平台，PostgreSQL 保持运行。正式应用正常日志在 `D:/ro-agent-data/logs/formal`，数据库日志位置由 `postgresql.log_file` 指定；测试运行日志名称以 `TEST-` 开头。

两种只读检查都不启停平台/数据库，不执行迁移、不生成启动日志：

```bash
bash /d/ro-agent-data/releases/start-formal.sh --check
bash /d/ro-agent-data/releases/start-formal.sh --check-db
```

第一条检查文件、配置和依赖；第二条额外连接已运行的数据库核对身份与迁移。它们不等于应用启动成功，更不等于真机验收。完整验收仍须实际页面提交任务，经真实数据库、模型和机器人，再核对三方记录。
