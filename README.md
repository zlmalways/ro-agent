# ro-agent

自然语言驱动机器人任务的独立 Agent 服务。首期一台机器人、一个活动任务；多机器人只预留绑定、执行单元和步骤依赖。

## 当前状态

2026-10-14 已更新数据边界技术文档：账号、客户成员、机器人登记和客户政策应由数据库动态维护，部署参数和平台秘密留在部署配置。当前仍是静态身份／政策装配；自带账号登录和完整在线管理尚未实现。见[完整清单](docs/data-configuration-boundaries.md)、[专项实施计划](docs/superpowers/plans/2026-10-14-data-configuration-boundaries.md)和[文档核对记录](docs/data-configuration-review.md)。文档更新不代表代码改造或新增验收通过。

已实现计划验证、任务受理、一次下发和反馈归约；PostgreSQL 事务、不可变记录、请求幂等、客户隔离；dlv/0 WebSocket、连接代次、隔离和未知结果恢复；Linux 主机锁加数据库独立会话锁。已提供配置、身份、政策和部署装配代码，但正式配置、真实提供方与真机未验收，不能称为已交付系统。

用户已批准首期按提交时间依次执行、排队任务不自动过期并保留到用户取消、开始前重新检查执行条件。队列依据数据库 `queue_seq`，不引入消息队列服务；真实凭据、供应商调用、真机、正式主机和锁目录验收仍有门禁。

身份／审批／资料阶段已实现并获用户确认：用户和机器人凭据分别绑定客户，资料维护需要 maintainer 权限，批准需要当前 approver 权限；每个动作明确配置 allow／require_confirmation／deny，缺配置返回 unavailable。计划范围、政策版本、到期和撤销均参与执行前检查；批准不能清除未知或冲突。资料每次保存新版本，并核对实际机器人目录与各字段观察时间。

统一文字／音频 API、GPT/DeepSeek 兼容规划适配器、豆包流式 ASR 协议、澄清／批准／取消和事实网页已实现。组合测试用真实 Edge、HTTP、PostgreSQL、WebSocket 与隔离协议模拟器走通提交 → 澄清 → 批准 → 两步反馈 → 事实报告；提供方和物理机器人使用明确隔离的测试夹具，没有把模拟结果视为真实服务或物理动作完成。

机器人语音输入、语音轮次、豆包 TTS 协议适配和 HTTP 流式降级入口已作为开发功能加入。用户已确认按机器人所属客户受理语音，澄清和批准走网页，机器人需要播报提示及结果。播报只读取持久化任务事实；跨重连重复语音规则尚未决定，缺少请求编号的自动受理默认关闭。语音增量通过了协议夹具和隔离 PostgreSQL/WebSocket 测试，但正式部署尚未装配 VoiceService、真实 TTS/ASR 凭据和语音绑定；原始 audio 帧和完整旧客户端兼容也尚未验收，不能视为生产语音能力。

提交前最新 Windows 全量测试结果为 138 项通过、5 项 Linux 专用测试跳过，静态检查通过。此前 Linux 核心测试 121 项通过，发生在独立审查修复和最新语音增量之前，不能替代当前版本的 Linux 回归。独立审查对核心分支发现的 9 个问题已修复并新增回归测试；最新语音增量不在该次独立审查范围内。

## 开发入口

- [架构与开发原则](AGENT.md)
- [V5 设计基线](docs/superpowers/specs/2026-10-12-robot-agent-v5.md)
- [独立阅读检查与本仓库适配](docs/superpowers/specs/2026-10-12-ro-agent-context.md)
- [首期实施计划](docs/superpowers/plans/2026-10-12-robot-agent-v1.md)
- [数据与配置边界专项计划](docs/superpowers/plans/2026-10-14-data-configuration-boundaries.md)（待另行批准代码实施）

实现基线：Python/FastAPI、PostgreSQL、单实例、兼容现有 dlv/0；模型与 ASR 是外部适配依赖。开发采用获准的 Superpowers 流程和 TDD，生产执行不降级到内存或假模型。

## 本地验证

Python 3.11+，在项目虚拟环境安装 `pip install -e ".[dev]"` 后运行 `python -m pytest -q` 和 `python -m ruff check src tests`。协议模拟和内存存储只在 tests/fixtures 中，不能配置为生产后备。

数据库测试必须明确设置 `RO_AGENT_TEST_DSN`，只接受 `127.0.0.1`／`localhost` 上名字以 `_test` 结尾的数据库。每例创建独立随机 schema，结束时只删除这个 schema；未设置时集成测试会显式跳过。只有配置真实测试数据库后运行的通过结果才证明 PostgreSQL 行为。

本次开发的隔离容器名为 `ro-agent-postgres-test`，本机端口 `127.0.0.1:55432`，数据库 `ro_agent_test`；密码和启动记录保存在被 Git 忽略的 `.superpowers/` 任务目录。数据位于容器临时内存盘，容器停止后不承诺保留测试数据，不用于业务或正式部署。
