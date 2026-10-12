# ro-agent

自然语言驱动机器人任务的独立 Agent 服务。首期一台机器人、一个活动任务；多机器人只预留绑定、执行单元和步骤依赖。

## 当前状态

仓库处于设计与实施计划审阅阶段，尚无可运行业务实现。未运行真机、部署或 T01—T26 / A01—A07 验收，不能将此仓库称为已交付系统。

## 开发入口

- [架构与开发原则](AGENT.md)
- [V5 设计基线](docs/superpowers/specs/2026-10-12-robot-agent-v5.md)
- [独立阅读检查与本仓库适配](docs/superpowers/specs/2026-10-12-ro-agent-context.md)
- [首期实施计划](docs/superpowers/plans/2026-10-12-robot-agent-v1.md)

实现基线：Python/FastAPI、PostgreSQL、单实例、兼容现有 dlv/0；模型与 ASR 是外部适配依赖。开发采用获准的 Superpowers 流程和 TDD，生产执行不降级到内存或假模型。
