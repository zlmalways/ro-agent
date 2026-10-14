# 机器人 Agent 首期实施计划

2026-10-14 状态补充：本页保留原阶段实施计划，不把未勾选项或初始待定描述当当前实现状态。后续开发和确认记录见 README／acceptance；数据、配置、账号登录及在线管理的新增工作以[专项计划](2026-10-14-data-configuration-boundaries.md)为准，未授权本轮实施代码。完整分类只在[数据边界清单](../../data-configuration-boundaries.md)维护。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户选择执行方式后使用对应流程，不自行扩大并行实现范围。

**Goal:** 交付一个独立单机器人自然语言任务服务，以完整计划、受控执行及可靠证据反馈形成闭环；真实物理交付需要单独获准联调。

**Architecture:** TaskService 组织任务用例与短事务，domain 定义合法变化，Preflight 复用验证与政策，Executor 归约指令／恢复事实。外部身份、模型、ASR、PostgreSQL 和 dlv/0 留在适配器；单实例、单执行槽，未知在途结果阻止新下发。

**Tech Stack:** Python 3.11+、FastAPI/Pydantic、PostgreSQL/asyncpg、httpx、pytest/pytest-asyncio。具体依赖版本在 Task 1 核实官方兼容信息后固定；不引入消息队列、向量库或通用多 Agent 框架。

**Deployment:** 用户已确认 Linux 单机进程；本地 Windows 可开发纯规则、API 和协议测试，但 Linux flock、双进程与断连保障必须在相应环境验证，不能把 Windows 测试称为 Linux 部署通过。

**Spec:** ../specs/2026-10-12-robot-agent-v5.md 和 ../specs/2026-10-12-ro-agent-context.md；后者补充新仓库引用、独立阅读发现和实施前置条件。

## Global Constraints

- 首版 1 台机器人、同时 1 个任务；任务排队自动执行，未来多机器人只保留执行单元／绑定／依赖。
- 先生成完整计划，检查及必要授权通过才执行；规划失败、权限不明或存储不可用时零指令。
- Python/FastAPI、PostgreSQL、单实例、不引入消息队列中间件；生产没有内存 Store 或 fake ASR/LLM 回退。
- 兼容 dlv/0、/api/voice/robot/ws、robot_id、x-robot-token、ready/hello/command/cmd_status/readiness/telemetry/cancel；不新增机器人字段、查询历史帧或 dlv/1。
- 多客户隔离、可信身份、不可变计划版本、批准范围与有效性检查、UTC 时间和版本条件更新。
- 未知结果、终态冲突、未知在途执行均阻止新下发；cancel 成功仅证明写入，不证明物理停止。
- 双锁依赖获准固定主机和共享锁对象；数据库所有权丢失不可在原进程自动恢复发送，跨主机不自动接管。
- API/模型候选不能提供可信 source_refs/客户/批准；服务端核实后产生执行证据。
- 模拟只在隔离 tests/ 中，并明确标注；仅运行模拟不能称为真实业务成功。
- 项目使用特性分支、获准 worktree；空仓库先保存文档基线提交，实施前按照 using-git-worktrees 征得隔离方式同意。不把主仓库和 managed worktree 混用。
- 提交用语义化前缀与中文；通过相关验收后按逻辑单元提交，不自动 push，不修改旧仓库工作区。
- B1—B7 未定的产品／配置／部署政策不能自行补默认值；相应生产行为拒绝或阻塞；P5 未通过不能宣称首期交付。

## Review Focus

1. 模型或 HTTP 在深层参数夹带 customer_id、approval、source_refs，不能越过白名单验证（Task 1/6）。
2. 下发标记提交后网络失败或服务重启，不能误判未发送再执行（Task 2/4）。
3. 旧终态冲突在下一任务启动后到达，必须隔离绑定且不复活历史任务（Task 2/4）。
4. hello 报告未知在途动作或不同租户 cmd_id，不能制造成功、解封或占用空闲（Task 3/4）。
5. 通知丢失、跨用户语音追问和排队批准失效，结果可重查且不能串任务或沿用旧批准（Task 4/5/7）。

## 结构与接口约定

创建 src/robot_agent/application/{task_service,preflight,executor,resources,input_service}.py；domain/{models,validation,transitions}.py；adapters/{postgres,dlv0,llm,asr,identity}.py；api/{tasks,resources,robot_ws}.py；ports.py、app.py、config.py、runtime.py。文件是职责边界，可用内部辅助函数拆解，但不增加状态写入者。

- TrustedContext(customer_id, actor_kind, actor_id)、TaskInput(request_id, channel, text, source_ref)。
- CandidatePlan(outcome, steps, question, required_slots, reason)；CandidateStep(step_id, action, args, depends_on)；不含可信证据。
- Plan／PlanStep、Task、ExecutionUnit、Command、RobotEvent、TaskEvent、ApprovalEvidence 等字段遵循 V5 第 4 节；ValidationResult、ChangeSet 是内部结果对象，不是新的持久状态。
- validate_plan(candidate: CandidatePlan, snapshot: ContextSnapshot, catalog: ActionCatalog) -> ValidationResult；check_transition(task: Task, command: Command | None, event: DomainEvent) -> ChangeSet。
- Preflight.check(context: TrustedContext, candidate_or_plan: CandidatePlan | Plan, snapshot: ContextSnapshot, phase: CheckPhase, approvals: list[ApprovalEvidence]) -> CheckResult；网络不在长事务中等待。
- Executor.prepare(context: TrustedContext, unit: ExecutionUnit, facts: ExecutionFacts) -> ChangeSet；apply_event(facts: ExecutionFacts, event: RobotEvent) -> ChangeSet；reconcile(facts: ExecutionFacts) -> ChangeSet。
- TaskService.submit(context: TrustedContext, value: TaskInput, request_key: str) -> TaskView；answer/decide/cancel 接收 V5 API 的 ID、expected_version 与幂等键；dispatch_next(deployment_id: str)、on_robot_event(event: RobotEvent)、recover(deployment_id: str) -> 用例结果。
- Store.unit_of_work(context: TrustedContext) -> AsyncContextManager[TaskTransaction]；TaskTransaction.load_facts(task_id: str, *, for_update: bool) -> ExecutionFacts；apply_changes(changes: ChangeSet) -> None；append_event(task_id: str, value: TaskEvent) -> None；request_key(operation: str, key: str, digest: str) -> RequestKeyResult；claim_slot(deployment_id: str, task_id: str, unit_id: str, binding_id: str, owner_run_id: str) -> bool。这些接口只做持久化与条件更新；RuntimeOwnerTransaction 是专用系统接口。
- RobotPort.send(command: Command) -> SendObservation；RobotPort.events() -> AsyncIterator[RobotEvent]；PlannerPort.plan(goal: str, answers: list[str], snapshot: ContextSnapshot) -> CandidatePlan；ASRPort.transcribe(audio: AudioInput) -> Transcript。

## Task 1：隔离的单任务最小链路与项目基础（P0）

**Files:** 创建 pyproject.toml、.gitignore、src/robot_agent/{__init__,ports}.py、domain/{models,validation,transitions}.py、application/{task_service,preflight,executor}.py；tests/unit/{test_minimal_flow,test_plan_validation}.py；tests/fixtures/ 中明确标记测试替身。

**Interfaces:** 产生上述模型、validate_plan、ChangeSet、TaskService.submit 和一次指令／反馈入口的初始实现；测试替身不得注入生产装配。

- [ ] 写 test_minimal_flow_completes_from_matching_feedback：submit 产生计划但无动作，检查通过和调度后一步发送，匹配成功后才能执行依赖步；最终 TaskView 对应两条成功反馈。
- [ ] 写 test_invalid_plan_never_dispatches：未知动作、虚构路点／技能、循环依赖、伪造客户或证据字段均 assert robot.sent == []；澄清输出也不发送。测试证据和机器人状态必须明确为 fixtures。
- [ ] 运行 python -m pytest tests/unit/test_minimal_flow.py tests/unit/test_plan_validation.py -q，确认缺接口导致预期失败，不能以环境错误当红灯。
- [ ] 实现最小纯规则及事务协作；声明运行依赖和测试依赖，创建项目隔离环境。开发 Python 使用可运行解释器，避免 WindowsApps 跳转别名；如需联网安装遵守沙箱审批。
- [ ] 重跑测试；P0 只证明软件链路。准备中文提交说明“feat: 建立机器人任务核心与隔离测试”，相关检查通过后提交。

## Task 2：真实 PostgreSQL 事务、身份隔离与幂等（P1）

**Files:** adapters/postgres.py、db/migrations/0001_robot_agent.sql、tests/integration/{conftest,test_store,test_command_transactions}.py。

**Interfaces:** 实现 Store/TaskTransaction；使用 V5 ra_ 表、租户复合关联、唯一键和 version 条件。TaskService 是任务写入入口，domain/Executor 返回 ChangeSet，适配器不自行选择状态。

- [ ] 写 test_request_key_idempotency：同键同内容同 task_id，同键不同内容 409；test_cross_customer_fk_rejected：跨租户关联拒绝。
- [ ] 写 test_failed_commit_never_sends 和 test_terminal_updates_atomically：模拟事务错误，不留半个成功步骤；重复终态只推进一次；迟到 running 不回退 succeeded。
- [ ] 在明确隔离测试库运行 python -m pytest tests/integration/test_store.py tests/integration/test_command_transactions.py -m integration -q，验证红灯；无测试库明确阻塞，不换内存冒充。
- [ ] 实现短事务、固定锁顺序、条件更新、请求摘要、持久事件；外部调用不在事务等待中。跨任务冲突先读取相关集合再排序加锁。
- [ ] 重跑单元和上述集成；验证 T05—T07、T11 事务部分。提交“feat: 持久化任务指令并落实事务隔离”。

## Task 3：dlv/0 契约与机器人连接（P2）

**Files:** protocol/dlv0_schema.py、adapters/dlv0.py、api/robot_ws.py、tests/contract/test_dlv0.py、tests/integration/test_robot_ws.py；docs/protocol-provenance.md。

**Interfaces:** RobotPort、RobotEvent、SendObservation、绑定和连接代次；协议定义从旧仓库 dlv_schema.py 选择性提取并记录来源提交 efb30b2/73918be（实际取用时核对），没有旧模块运行导入。

- [ ] 写 test_dlv0_frames_unchanged：ready、hello、command、cmd_status、cancel 形状与原定义一致，绝不发送 dlv/1 或历史查询。
- [ ] 写 test_stale_connection_does_not_update、test_cross_customer_cmd_rejected、test_hello_id_mismatch_rejected、test_unknown_busy_isolates：错误来源不能推进，未知在途执行持久隔离，busy=false 不解封。
- [ ] 运行 python -m pytest tests/contract/test_dlv0.py tests/integration/test_robot_ws.py -q（集成标记按环境选择）并确认预期红灯。
- [ ] 实现单读取循环、受认证绑定、消息白名单、字段证据时间、写入门；合法事件进入 TaskService，驱动不写任务表。与 Task 2 真库及测试协议模拟器组合，模拟器只在测试运行。
- [ ] 验证 T08—T12、T14/T15 来源部分及 A03；提交“feat: 接入兼容 dlv零版机器人通信”。

## Task 4：唯一执行权、队列与重启恢复（P3）

**Files:** runtime.py、application/executor.py、application/task_service.py、tests/integration/{test_runtime_owner,test_recovery,test_queue}.py、tests/unit/test_event_priority.py。

**Interfaces:** RuntimeOwnership.acquire/healthy/close，固定主机共享锁与独立 PostgreSQL 会话锁；QueuePolicy 排序／失效／通知规则需实际批准。Executor.reconcile 复用 apply_event 与 domain。

- [ ] 写 test_dispatch_crash_window_unknown：PREPARED 不制造在途未知，DISPATCHING/SENT 没证据禁止重发；新连接可靠终态可恢复、旧连接迟到不可推进。
- [ ] 写 test_duplicate_owner_cannot_send、test_db_lock_loss_disables_old_process、test_lock_files_shared_by_deployment；须用两个真实进程，不能只测布尔变量。
- [ ] 写 test_late_conflict_quarantines_active_binding、test_expired_approval_waits_and_does_not_repeat_completed_steps、test_unknown_keeps_slot；队列测试用明确 TestPolicy，不视作产品 FIFO 批准。
- [ ] 运行对应测试红灯，平台／真库不足则标明未验，不削弱双锁或未知阻塞。
- [ ] 实现运行所有权门、唯一任务槽、受批准队列规则和恢复；无 B3 政策则生产出队拒绝，保留已受理任务；UNTRACKED_EXECUTION 默认拒绝解封。
- [ ] 运行 T13—T18、T23 后端部分、A04—A06；正式部署需平台与锁目录确认。提交“feat: 实现受控调度与证据恢复”。

## Task 5：身份、授权证据和资料管理（P4 前置）

**Files:** adapters/identity.py、application/resources.py、application/preflight.py、api/resources.py；tests/unit/test_policy.py、tests/integration/{test_authorization,test_resources}.py。

**Interfaces:** IdentityPort.resolve_web/resolve_robot、PolicyPort.evaluate、ResourcesPort.snapshot，CheckResult 在 TaskService 事务保存。B2/B5 不明确时 unavailable，不设置默认全许可。

- [ ] 写 test_approval_is_plan_scoped：批准旧计划不能放行新版本；当前 deny/unavailable 覆盖旧批准；撤销、到期和政策版本变化后零下发。
- [ ] 写 test_resource_versions_immutable 和 test_readiness_not_refreshed_by_unrelated_telemetry：资料变更不静默替换在途参数，旧证据不因无关更新变新。
- [ ] 运行上述测试确认红灯；实现可信身份、资料维护权限、服务端 source_refs、版本与新鲜度检查，绝不信文本/member_id 自报身份。
- [ ] 跑 T03、T06、T09、T14、T16/T17；提交“feat: 校验身份授权与可追踪能力资料”。

## Task 6：模型与 ASR 适配、统一任务 API（P4）

**Files:** adapters/{llm,asr}.py、application/input_service.py、api/tasks.py、app.py、config.py；tests/contract/{test_planner,test_asr}.py、tests/api/{test_tasks,test_task_audio}.py。

**Interfaces:** PlannerPort/ASRPort 和 V5 第 6 节 API；认证路由统一 TaskService，不增加 ReAct 物理执行旁路。提供方与数据外传政策决定实际接入。

- [ ] 写 test_planner_rejects_reserved_fields、test_unavailable_provider_never_creates_executable_task、test_audio_and_text_share_submit、test_same_key_audio_retry_returns_existing_task。
- [ ] 写 test_api_conflict_and_visibility：expected_version 冲突409、跨客户对象404、未认证401、依赖不可用503；无权审批403。
- [ ] 运行 python -m pytest tests/contract tests/api -q 确认相应红灯；供应商测试使用 HTTP 协议夹具，不误报真实提供方成功。
- [ ] 实现已批准供应商、大小／超时限额、转写、错误分类与输出校验；敏感配置来自环境引用，生产缺配置显式关闭相关能力。网络重试不能导致重复物理下发。
- [ ] 跑 T01、T19—T22、A01—A02；有授权配置时单独执行实际提供方验收，否则明确未验。提交“feat: 统一自然语言与语音任务入口”。

## Task 7：用户端完整链路与事实报告（P4）

**Files:** client/ 中最小网页、api/tasks.py 结果视图、adapters/dlv0.py 语音输出映射；tests/e2e/test_task_flow.py、tests/contract/test_voice_output.py。

**Interfaces:** TaskView、TaskEvent 游标和 result_summary 按 V5；最小 Web 页使用标准 HTML/JavaScript 调 HTTP，不为简单页面引入旧 React 平台。后续视觉设计另行确认，先保证必要操作可用。

- [ ] 写 test_submit_clarify_approve_observe_result：通过真实 API、PostgreSQL 和协议模拟器完成文字任务；检查计划、审批、步骤、结果一致。
- [ ] 写 test_event_replay_after_lost_notification、test_other_speaker_cannot_answer、test_voice_round_ends_on_error：通知丢失可重查、跨用户不串任务、meta/done/idle/error 正确收尾。
- [ ] 运行对应端到端红灯；测试明确使用隔离模拟机器人，不能拿它说用户真机已收到结果。
- [ ] 实现最小提交、澄清、批准、取消、进度与结果页面；结果只读取持久事实，未知和争议必须可见，不让模型自行宣布成功。
- [ ] API 与浏览器实际交互组合验证，T20—T23、A07；生产播报／HTTP 降级兼容受 B4 决定。提交“feat: 提供可追溯任务交互与执行报告”。

## Task 8：受控部署与真机验收（P5）

**Files:** docs/acceptance.md、docs/deployment.md、适合已批准平台的部署入口、tests/acceptance/（默认不连接真机）。

**Interfaces:** 不增加新业务模型；只验收所有已实现契约与 B1—B7 条件。

- [ ] 先核对 B1—B7 的实际资料、权限、成本／外传规则、部署平台及锁对象；缺一项就记录受影响验收，不读取未批准凭据。
- [ ] 写验收场景：拿杯水仅“到桌边、拿杯”，再用另一种已支持任务证明通用性；写断线恢复、有结果未知保持阻塞、用户报告可追踪、客户隔离的预期观察。
- [ ] 确认真机授权对象与指令范围后才运行；记录 T24—T26、适用 A 场景和部署双进程结果，未获授权不执行。
- [ ] 回归 T01—T26 和 A01—A07，标明每项单元／集成／模拟／实际供应商／真机证据，不能把分别通过的单项表述为组合通过。
- [ ] 完成代码审查，修复本任务问题，按完成前验证核实 Git 状态及测试输出；提交“docs: 记录首期实际验收与部署限制”。推送／合并需另按用户授权。

## 自检与启用门禁

- 需求覆盖：R1/10→Task1/2/6/8；R2→6/7；R3→1/5；R4→3/7；R5→4；R6→2/5；R7→5；R8→1/3；R9→4；R11→2/7；R12→6/8 测量不许诺；R13→4/7。
- T01—T26 全部映射上述任务，A01—A07 映射适配变化、来源与恢复测试。TDD 的红灯必须是相应行为缺失，不是装依赖失败。
- 方法和类型沿用“结构与接口约定”；实现时低风险内部选择可在任务中说明，产品行为／部署保障变动需确认。
- 独立 builder-check 已指出三项条件，context 文档保存它们；没有声称全规格独立通过或测试已运行。
- 正式部署记录为 Linux 单机进程；原队列待定项已有后续确认记录，见总规格第 8、13 节。本次账号／动态管理专项的产品细节和代码实施仍待确认；缺供应商、政策或真机条件的能力保持关闭，不把 P0 当交付。
