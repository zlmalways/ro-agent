# ro-agent

自然语言驱动机器人任务的独立 Agent 服务。首期一台机器人、一个活动任务；多机器人只预留绑定、执行单元和步骤依赖。

## 当前状态

P0—P3 已实现：计划验证、任务受理、一次下发和反馈归约；PostgreSQL 事务、不可变记录、请求幂等、客户隔离；dlv/0 WebSocket、连接代次、隔离和未知结果恢复；Linux 主机锁加数据库独立会话锁。Windows 上 63 项测试通过，5 项平台测试明确跳过；另外在 Linux 容器中 5 项锁测试通过，包括真实第二进程、数据库锁断连和跨主机拒绝。真实机器人、规划与政策仍用隔离测试夹具，生产装配／模型／身份接入尚未完成；不能称为已交付系统。

用户已批准首期按提交时间依次执行、排队任务不自动过期并保留到用户取消、开始前重新检查执行条件。队列依据数据库 `queue_seq`，不引入消息队列服务。尚未获准的身份／政策／供应商启用，以及真机、正式主机和锁目录验收仍有门禁。

身份／审批／资料阶段已实现并获用户确认：用户和机器人凭据分别绑定客户，资料维护需要 maintainer 权限，批准需要当前 approver 权限；每个动作明确配置 allow／require_confirmation／deny，缺配置返回 unavailable。计划范围、政策版本、到期和撤销均参与执行前检查；批准不能清除未知或冲突。资料每次保存新版本，并核对实际机器人目录与各字段观察时间。Windows 上 86 项测试通过、5 项 Linux 测试跳过；此前 Linux 锁测试独立通过。

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
