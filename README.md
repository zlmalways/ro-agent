# ro-agent

自然语言驱动机器人任务的独立 Agent 服务。首期一台机器人、一个活动任务；多机器人只预留绑定、执行单元和步骤依赖。

## 当前状态

P0 核心与 P1 数据库阶段已实现：计划验证、任务受理、单指令下发和反馈归约，以及 PostgreSQL 事务、不可变计划／指令、请求幂等、客户隔离和反馈证据持久化。37 个测试通过，其中 18 个使用真实 PostgreSQL 16 容器；机器人、规划与政策仍为明确隔离的测试夹具，没有生产应用入口或真实模型接入。未运行真机、Linux 双锁部署或完整 T01—T26 / A01—A07 验收，不能将此仓库称为已交付系统。

## 开发入口

- [架构与开发原则](AGENT.md)
- [V5 设计基线](docs/superpowers/specs/2026-10-12-robot-agent-v5.md)
- [独立阅读检查与本仓库适配](docs/superpowers/specs/2026-10-12-ro-agent-context.md)
- [首期实施计划](docs/superpowers/plans/2026-10-12-robot-agent-v1.md)

实现基线：Python/FastAPI、PostgreSQL、单实例、兼容现有 dlv/0；模型与 ASR 是外部适配依赖。开发采用获准的 Superpowers 流程和 TDD，生产执行不降级到内存或假模型。

## 本地验证

Python 3.11+，在项目虚拟环境安装 `pip install -e ".[dev]"` 后运行 `python -m pytest -q` 和 `python -m ruff check src tests`。协议模拟和内存存储只在 tests/fixtures 中，不能配置为生产后备。

数据库测试必须明确设置 `RO_AGENT_TEST_DSN`，只接受 `127.0.0.1`／`localhost` 上名字以 `_test` 结尾的数据库。每例创建独立随机 schema，结束时只删除这个 schema；未设置时集成测试会显式跳过。只有配置真实测试数据库后运行的通过结果才证明 PostgreSQL 行为。

本次开发的隔离容器名为 `ro-agent-postgres-test`，本机端口 `127.0.0.1:55432`，数据库 `ro_agent_test`；密码和启动记录保存在被 Git 忽略的 `.superpowers/` 任务目录。数据位于容器临时内存盘，容器停止后不承诺保留测试数据，不用于业务或正式部署。
