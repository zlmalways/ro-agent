# 机器人 Agent 服务开发设计与实现规格

版本：V5 开发设计草案。日期：2026-10-12。基于 V4（2026-10-09）按 AGENT.md 第 8.1 节收敛职责、事实写入和验证边界。

本文是开发入口：说明要实现的系统、模块接口、数据结构、状态转换、兼容约束、开发顺序和验收测试。阅读本文不需要此前的聊天记录。本文不授权修改业务代码、生产配置或机器人端；实际开发另行确认范围。

**状态说明：**第 1 节是用户已确认要求；第 2 节是从当前仓库核实的兼容事实；其余目录、内部接口、数据表和新增 HTTP API 是本稿提出的开发方案，供评审。第 13 节列出的外部资料或业务决策未解决前，不能宣称已经能在真机上端到端交付。待确认不等于允许开发者自行猜测。

**V5 的评审方式：**“更优雅”指在同等需求和保障下，职责更容易解释、变化影响更局部、状态决定权更明确；不是文件更少或版本号更新的证明。第 16 节列出每项取舍、代价、可反驳条件和对应验收。V4 的历史独立审核不自动覆盖 V5。本次只修改设计文档，尚未实现或运行这些验收。

## 1 目标和不可改变的边界

目标是建设通用机器人 Agent 服务：接收自然语言任务，生成完整计划，检查通过并获得必要授权后执行。计划保存在云端，逐步下发指令，依据机器人执行反馈推进，遇到问题组织处理。

| 编号 | 已确认要求 |
| --- | --- |
| R1 | Python 后端，FastAPI 接入；Agent 内部框架不预先指定 |
| R2 | 输入来自 Web 端或机器人端；首版支持文字与语音，通过外部 ASR API 将语音转成文字 |
| R3 | **先生成完整计划，检查通过后再执行**；不等于每次都必须人工确认 |
| R4 | **必须兼容现有 dlv/0，不修改机器人端协议**；机器人主动连接 Agent 服务 |
| R5 | 首版 1 台机器人、同时 1 个任务；下一阶段不超过 10 台机器人，架构覆盖分工、等待、交接 |
| R6 | 多个独立客户使用，客户数据、机器人和权限必须隔离 |
| R7 | Agent 服务维护能力资料与环境资料；实际机器人能力和执行事实仍须有可信依据 |
| R8 | 业务动作交给机器人；操作门禁等其他业务系统由机器人负责，云端不增加这些执行对象 |
| R9 | 接受故障期间暂停下发；恢复后核实实际状态，在授权范围内自动继续；结果未知时不盲目重发 |
| R10 | 首版实现基线已确认：PostgreSQL、单运行实例、不引入消息队列中间件；业务任务可以在 PostgreSQL 中排队 |
| R11 | 追踪用户请求、计划、检查结果、指令及执行反馈；区分已生成、已下发、机器人已接收、动作已完成 |
| R12 | 尽量降低延迟，但没有已确认的毫秒级时限；不能为了更早启动动作绕过检查 |
| R13 | 当前任务尚未结束时，新独立任务需要排队，之后自动执行；当前任务的补充回答仍归原任务 |

ASR 是语音识别；客户是需要独立隔离的组织或服务使用方，用户是提出任务的人。动作是机器人要完成的操作，指令是要求机器人执行动作的请求。执行单元是分配给一台机器人完成的一组步骤。

用户提供的说明案例是：“帮我去拿杯水”——移动到桌子边上，拿取水杯；不包含倒水或送回用户身边。它是检验通用流程的例子，不建立拿水专用模块，不将例子当成真机能力证明。

视觉、语音播报、紧密同步的多机器人动作、跨客户协作均不是已确认的首版能力。共享还是客户专属部署未定；单运行实例不代表所有客户必须共享数据库。下一阶段任务并发数也不能从机器人数量推导。

## 2 已有实现提供的兼容事实

本节描述当前仓库，不代表当前机器人部署已实测通过。旧实现行为与 R1—R13 冲突时，保留兼容消息格式，不直接复制旧行为。

### 2.1 机器人通信基线

必须保留 WS 路径 /api/voice/robot/ws、查询参数 robot_id，以及已有 x-robot-token 请求头的接入形式。不能要求机器人新增 customer_id、task_id、plan_id 或新认证消息。客户归属由服务端认证绑定确定，见第 5 节。

| 方向 | dlv/0 消息 | 核心内容及用途 |
| --- | --- | --- |
| 机器人端 → Agent 服务 | hello | robot_id、capabilities、skills；可有 executor、busy_cmd_id，建立命令连接能力快照 |
| Agent 服务 → 机器人端 | command | type、skill、args、cmd_id，下发一个动作 |
| 机器人端 → Agent 服务 | cmd_status | cmd_id、state；可有 detail、error_code，报告对应指令的状态 |
| 机器人端 → Agent 服务 | readiness | 就绪快照；可有 waypoint_names、brain_skills、busy 等已有字段 |
| 机器人端 → Agent 服务 | telemetry | battery、location、observed_at 等测量信息 |
| Agent 服务 → 机器人端 | cancel | type、cmd_id，请求取消；发送成功不证明机器人停止 |

cmd_status.state 已定义 pending、running、succeeded、failed。仅 succeeded 和 failed 是旧协议的终态。新云端可以保留中间状态和证据，但不能要求机器人一定按顺序发送每个状态。

dlv/1 的 accepted、progress、cancel_ack 等消息只是预留声明，当前运行路径没有接入。不得启用 dlv/1，不得新增“查询指令历史”消息来补恢复缺口。

建立连接后保留现有 ready 消息：
```json
{"type":"state","state":"ready","protocol":"dlv/0","robot_id":"<连接的 robot_id>"}
```
有效 hello 注册完成后保留命令连接 ready 消息：
```json
{"type":"state","state":"ready","channel":"command","robot_id":"<hello.robot_id>"}
```
尖括号表示来自已核实数据的占位说明，不是可以发给真机的取值。hello.robot_id 必须与连接查询参数一致，并属于认证绑定允许的标识。

### 2.2 动作词表不是实际技能清单

唯一协议定义来源是 src/domains/robots/dlv_schema.py。启动装配将它转换为业务层可用的 ActionCatalog，不另手写一份相互漂移的协议词表。

| 分类 | 动作与必需参数 | 新系统处理 |
| --- | --- | --- |
| 导航 | move_to(location) | location 必须解析为机器人实际支持的导航路点 |
| 具体操作技能 | vla_skill(skill_id) | skill_id 来自该机器人上报的 brain_skills，经服务端能力与权限检查后使用 |
| 其他机器人动作 | deliver(object, person)、look_at(target)、capture_image()、wait(seconds) | 只有真实能力资料与机器人声明均支持，且授权允许时才可执行 |
| 兼容旧记录 | grasp、place、pick、press | 不生成到新计划；旧记录如何迁移另行设计，不自动恢复执行 |
| 机器人管理操作 | list_waypoints、save_waypoint | 不进入用户任务规划动作集；本稿不新增其调用入口 |
| 旧云端代办 | speak、deliver_artifact、notify_requester | 不作为机器人 command 下发，也不自动纳入本次业务范围 |

handover 是 deliver 的已有别名。内部使用协议定义的规范名称；下发时选择机器人已声明且协议允许的名称：声明 deliver 则用 deliver，仅声明 handover 则使用已有别名映射，不凭空改动作名称。

有效能力必须同时满足：服务端配置允许、机器人声明支持、具体技能目录能够确认。旧机器人没上报 skills 时不能推断“支持全部”；新云端应报告 CAPABILITY_UNKNOWN，等待核实。

readiness 中的技能和路点只能用于核实或收窄能力，不能扩大权限。busy=false 或 telemetry.location 看似正确，不足以证明某条指令完成。

执行前检查使用同一快照中的就绪字段：move_to 要求 nav_server_ready=true；vla_skill 要求 brain_server_ready=true 且 brain_engine="ready"；属于协议 REAL_SKILLS 的其他动作要求 skill_server_ready=true。其他动作必须有经核实的就绪判断配置，否则阻塞。

每个必要就绪字段保存对应证据的服务端 received_at 和 connection_generation；执行时要求来自当前有效连接且未超过经评审配置的新鲜度上限。快照重建、无关 telemetry 或其他字段更新不能刷新该字段的证据时间；新连接不能沿用旧连接的就绪证据。必要字段缺失、过期或值不满足条件均阻塞。这里沿用既有消息字段，不增加机器人消息，也不假定真机上报周期。

旧 wait 动作上限是 240 秒。它不是任务总超时。实际指令超时值由配置决定并需验证，不能把旧路由默认值当作用户响应承诺。

### 2.3 语音连接也属于兼容范围

同一路径既有语音连接也有命令连接；不能将所有消息都解释成 command。接入模块每条 WS 连接仅有一个读取循环，按已识别的连接用途分发。

- 语音上行：audio.pcm_b64 为原始 16 kHz 单声道 PCM；segment.wav_b64 为已分段 WAV。保留现有字段解析，不要求客户端换消息格式。
- 原始音频经分段后进入统一输入处理模块；ASR API 返回的最终文字才参与任务规划，中间识别结果不触发动作。
- member_id、guest_id、confirm_speaker、woke 等客户端字段是输入提示，不授予身份或权限。是否采用声纹身份来源、唤醒规则以及谁可发起任务，见 B2、B4。
- 旧语音交互是半双工：播报期间闭麦，interrupt 不代表任务取消，也不能实现动作急停。语音 stop 消息用于停止播放，不等于 cancel 指令。
- 保留 thinking → meta／可选 audio → done → idle/error 的语音轮次封装；成功、拒绝和异常路径都必须结束轮次，防止机器人持续闭麦。meta 使用现有 transcript、awake 字段；done 保留 text、reply、transcript、robot_id、is_question、expects_reply、play_secs 以及现有身份结果字段。身份未确认时不得填入虚构成员。
- 是否启用语音播报仍待确认。不得为了让机器人“有反应”伪造音频或成功反馈；机器人端是否展示文字结果、无播报时的交互可用性需联调验证。
- 已有机器人 HTTP 降级入口是否也需兼容列为 B4，不能在迁移中静默删除。

语音轮次代表一次输入与回答，不代表整个物理任务。不得一直保持 thinking 等待机器人执行完整任务。命令连接的注册不应依赖 ASR 或语音播报服务可用。

### 2.4 可复用代码与不能照搬的行为

| 来源 | 用途 | 注意事项 |
| --- | --- | --- |
| src/domains/robots/dlv_schema.py、primitives.py | 协议动作、能力和别名定义 | 使用实际 skills 与 brain_skills 过滤，不把全局词表当真机目录 |
| src/api/routes/voice_ws.py、robots/ws_registry.py | WS 消息字段、接入格式和路由 | 仅参考兼容格式；客户绑定、业务分发按本规格重做 |
| src/domains/robots/ws_driver.py | command、cmd_status、cancel 的编码处理 | 不沿用“断线直接失败”或“重连 busy 就取消”的恢复行为 |
| src/domains/voice/asr.py、_doubao_asr.py | 现有 ASR 接口与豆包 API 适配 | 本地 Qwen 不是外部 API；fake 只可用于测试，不作生产降级 |
| src/llm/base.py、openai_client.py | 已有大模型调用接口 | 具体提供方未定；不复制与机器人任务无关的旧业务 |
| pyproject.toml、docker-compose.yml | Python 依赖及 PostgreSQL 基础 | 不因已有 pgvector 就认定新服务需要向量检索 |

当前分支存在 AGENT.md 与 CLAUDE.md；前者第 8.1 节提供本稿的架构开发原则。EXTERNAL_REVIEW.md、AGENTS.md 和 .env.example 在当前快照中不存在，不能将它们作为当前证据。部署启用登记、机器人端实际技能目录和配置仍需另行核实。README 的模型示例不能证明线上配置。开发只可使用脱敏测试配置，不读取或使用生产凭证进行联调。

## 3 模块与建议代码位置

以下位置是新增实现的提案，不是已经存在的文件。src/robot_agent/ 表示独立的应用包边界；无论最终在本仓库还是独立仓库实现，都必须有自己的启动装配、配置与测试，不能因同仓库而导入旧 TaskService、旧 Agent 回合编排或旧 wiring。仓库落点与发布切换另行确认，第 14 节说明集成边界。真正启用时，同一路径只能挂一个机器人 WS 处理器；不能让新旧执行器同时控制机器人。

```text
src/robot_agent/
├── app.py                     # 应用工厂和启动装配
├── config.py                  # 配置读取与缺失检查
├── api/
│   ├── tasks.py               # 新增任务、澄清、查询、授权决定入口
│   ├── robot_ws.py            # 保持 dlv/0 路径的唯一 WS 读取循环
│   └── resources.py           # 能力资料和环境资料管理入口
├── application/
│   ├── input_service.py       # 文字／语音归一化
│   ├── task_service.py        # 唯一任务用例入口，组织事务、队列与状态落库
│   ├── preflight.py           # 编排验证与授权，返回检查依据，不修改执行状态
│   ├── executor.py            # 指令准备、一次下发、反馈归约与恢复核对
│   └── resources.py           # 资料版本维护与快照读取
├── domain/
│   ├── models.py              # 第 4 节对象及状态
│   ├── validation.py          # 动作、依赖、参数、事实依据检查
│   └── transitions.py         # 第 7 节合法状态变化
├── ports.py                   # 第 5 节接口，不依赖具体网络或数据库库
├── adapters/
│   ├── identity.py            # 身份来源与客户／机器人绑定
│   ├── asr.py                 # 外部 ASR API
│   ├── llm.py                 # 大模型 API
│   ├── dlv0.py                # 已有消息编码、解析及连接管理
│   └── postgres.py            # PostgreSQL 事务及存储接口
└── runtime.py                 # 进程生命周期、所有权与唤醒，调用任务用例
tests/robot_agent/             # 契约、状态、事务和接入测试
```

“接入层”调用 application；application 使用 domain 和 ports；adapters 实现 ports；app.py 连接具体实现。domain 不导入 FastAPI、网络或数据库库。模型生成文本不能直接调用 dlv0.py。

### 3.1 各模块负责什么、不负责什么

文件名是建议，下面的职责与调用约束才是契约。内部协作可以使用函数和数据对象，不给每个模块都加一层 Port、注册器或独立服务。

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| api/tasks.py、input_service.py | 认证上下文、文字／音频归一化，调用同一个 TaskService | 不复制计划、授权、状态推进规则 |
| TaskService | submit、answer、decide、cancel、dispatch_next、on_robot_event、recover 等用例；组织一次完整事务和用户视图 | 不编码协议、不解析模型供应商响应、不实现每种动作 |
| Preflight | 组合 domain 验证、Resources 快照和 PolicyPort，返回检查／授权记录及服务端证据 | 不自建状态机、不写 Command、不发送指令 |
| Executor | 指令生命周期的实施细节：生成准备内容、接纳反馈、核对恢复、执行已提交的一次发送 | 不受理用户新任务、不直接改任务目标、不建立第二个调度器 |
| domain | 数据约束、合法变化与结果归约；输入事实得到变化集合或拒绝原因 | 不访问网络、SDK、数据库，不自行猜测事实 |
| Resources | 资料版本维护、来源核实、快照读取 | 不因机器人声明或模型文本扩大权限 |
| adapters/dlv0.py | 当前认证连接、连接代次、协议编解码、单读取循环、输出映射和最终写入门 | 不决定任务完成、不重试动作、不直接更新任务表 |
| adapters/postgres.py | 客户范围内事务、条件更新、唯一键、锁与持久化 | 不自行决定业务状态或执行网络操作 |
| runtime.py | 获得／失去进程执行权、启动与关闭、唤醒任务用例 | 不选另一套队列、不清空未知占用、不把重启当动作停止 |

api/resources.py 与 application/resources.py 分别是接入转换和资料业务，不因同名而合并。PlanValidator 是 domain 验证函数／对象；PolicyPort 是政策依赖接口；Preflight 组合二者，不能代替或复制它们的规则。恢复入口归 TaskService，证据核对归 Executor，合法变化归 domain；若实现太长可以抽内部辅助文件，但不能形成新的决定权或状态副本。

### 3.2 谁能决定与持久化状态

TaskService 是任务类写用例的唯一入口，不是把所有算法堆进去的万能服务。它在同一 Store 事务中读取客户、版本和占用，调用 domain／Executor 得到变化集合，再通过受限事务接口落库。下面的“写入负责人”表示用例所有权，SQL 由 PostgreSQL 适配器执行。

| 记录 | 决定来源 | 唯一写入负责人 |
| --- | --- | --- |
| Task、Clarification、Plan、ExecutionUnit、执行槽 | domain 的合法变化；计划由 Planner 返回候选，经 Preflight 核实 | TaskService 相应任务用例；计划采纳后不可原地改写 |
| Command、关联的 RobotEvent、TaskEvent | Executor 核对证据，domain 归约指令和任务结果 | TaskService 的下发／反馈／恢复用例，在同一事务更新相关记录 |
| ValidationRecord、AuthorizationDecision、ApprovalEvidence | Preflight 和 PolicyPort 给出判断；批准用例核实审批者与版本 | TaskService 采纳或准备下发的事务；拒绝依据同样保存 |
| RobotBinding 归属与凭据许可、ResourceVersion | Resources 与身份管理的已授权维护规则 | 资料／绑定维护用例；不开放任意客户字段更新 |
| RobotBinding 证据冲突隔离与 Task.result_disputed | 第 7.3 节冲突规则 | TaskService 反馈事务；不能由后台清理、重连或普通资料更新解封 |
| ra_runtime_owner | 第 8 节固定主机运行所有权规则 | Runtime 的专用系统接口；不能修改客户任务／指令状态 |

机器人读取循环解析并传递事件到 TaskService.on_robot_event；只有该用例完成来源校验与事务才能改变执行记录。未经关联的消息只走受限诊断。事件通知在事务提交后发出；漏通知可从持久化事件游标重新读取。TaskEvent 是展示依据之一，不是一个重放后另行控制机器人的事件溯源系统。

TaskService 可以依赖 Executor，Executor 不能反向调用 TaskService。Executor 的 apply_event／reconcile 等方法只返回包含指令、步骤和任务结果的变化集合；不先提交 Command 再通过回调提交 Task。用户取消、下发标记、反馈与绑定隔离采用统一锁约定：先锁部署执行槽，核实相关任务集合，再按稳定 ID 顺序锁任务、执行单元／指令与绑定。所有相关写入口必须遵守同一约定；不能先锁旧任务，再在迟到冲突分支反向锁下一任务。最终锁范围与隔离级别由 PostgreSQL 集成测试验证，不能仅凭内存单线程证明原子性。

### 3.3 外部接口少，内部协作直接

Port 只用于确实需要隐藏的变化：身份／政策来源、ASR／LLM 提供方、RobotPort、Store。TaskService、Preflight、Executor 和 domain 通过内部函数或对象协作，不实现通用插件发现、服务定位器、事件总线调度或多 Agent 编排。启动装配只注入组件；运行上为一个应用实例，并非每个文件一个微服务。

## 4 内部数据契约

以下是本稿提出的内部格式，不发送给机器人。ID 使用不透明字符串；新内部实体 ID 建议 UUID，时间保存 UTC。所有业务持久实体都携带 customer_id，业务访问从已认证上下文取得 customer_id，而非信任请求或大模型传值。第 8 节的部署运行所有权是系统级元数据，不属于业务租户数据，不向客户 API 暴露。

| 对象 | 必需内容与约束 |
| --- | --- |
| TrustedContext | customer_id、actor_kind(user/robot)、actor_id、认证来源、授权检查所需引用；由 IdentityPort 创建，普通业务输入不能构造可信身份 |
| TaskInput | request_id、channel(web/robot)、text、source_ref；text 非空；source_ref 关联录音处理记录或连接轮次，但不是身份依据 |
| Task | task_id、customer_id、actor、goal_text、state、reason_code、version、active_plan_id、result_disputed、created_at、updated_at；version 用于并发更新检查；result_disputed 独立表示结果证据存在争议，不重开终态任务 |
| Clarification | clarification_id、task_id、question、required_slots、state(open/answered)、answer_ref；回答只能用于所属任务 |
| ContextSnapshot | snapshot_id、客户、能力版本、环境版本、robot_binding_id、机器人连接代次、skills、brain_skills、waypoint_names、readiness、readiness_evidence、observed_at；readiness_evidence 按字段记录 received_at 与 connection_generation，observed_at 不能替代证据时间；不含其他客户数据 |
| Plan | plan_id、task_id、revision、outcome(plan/clarify/unsupported)、steps、snapshot_id、validation_id；获准版本不可原地覆盖 |
| PlanStep | step_id、action、args、depends_on、source_refs；source_refs 由服务端在接地检查后产生，关联能力／环境版本及条目；模型不得提供可信证据或身份字段 |
| ExecutionUnit | unit_id、task_id、plan_id、robot_binding_id、state、next_step_id、version；首版只有一个执行单元，步骤串行 |
| Command | command_id、unit_id、plan_id、step_id、attempt、wire_robot_id、wire_skill、wire_args、dispatch_state、outcome、dispatch_generation、owner_run_id、evidence_conflict、created_at、sent_at、receipt_observed_at、finished_at、version |
| ValidationRecord | validation_id、客户、task_id、plan_id、可选 step_id、检查项及结果、资料版本、规则版本、检查时间；由 Preflight 产生，TaskService 在采纳／下发事务中保存 |
| AuthorizationDecision | decision_id、客户、actor、task_id、plan_id、可选 step_id、base_result、effective_result、理由、策略版本、approval_refs；结果取 allow/deny/require_confirmation/unavailable，不可用不等于允许 |
| ApprovalEvidence | approval_id、客户、task_id、plan_id、plan_revision、scope、approver_id、policy_version、issued_at、expires_at、revoked_at；scope 由服务端确定，调用者不能自行批准超出范围的步骤 |
| RobotEvent | event_id、客户、robot_binding_id、connection_generation、收到时间、type、cmd_id、state、经白名单保留的详情；原始密钥和任意大体积内容不落日志 |
| TaskEvent | task_id、递增 seq、event_type、当前 task_version、对该用户可见的摘要；用于进度查询，不驱动另一个执行状态副本 |

这些对象是需要表达的业务信息，不是一对象一服务、一对象一表的要求。Clarification 可以属于 Task；PlanStep 可以属于不可变 Plan；ValidationRecord、AuthorizationDecision 与 ApprovalEvidence 可以存入带 kind 和严格约束的 ra_decisions。不得因合表而失去审批范围、外键、版本或不可变证据。保留 ra_units 的首版单行执行单元，提供命令归属和机器人占用，不实现跨机分配策略；它没有验收过的额外状态不得重复维护 Task 与 Command 已有事实。Task.state 是用户任务的权威状态；执行单元进度是同一事务内更新的恢复游标，页面不再以它推导另一套任务状态。

能力资料包含服务端允许的动作和参数约束、具体技能 ID 的业务含义；环境资料包含可验证的目标、导航路点映射和来源。资料管理不因 hello 自报而扩大服务端授权。实际状态记录是带时间的观察，不保证永远等于机器人当前情况。

### 候选计划格式

PlannerPort 只返回下列判别结构之一：
- plan：完整 steps；不得夹带未完成步骤或待确认目标。
- clarify：question 与 required_slots，steps 为空。
- unsupported：reason，steps 为空。

plan 的说明示例：
```json
{
  "outcome": "plan",
  "steps": [
    {"step_id": "s1", "action": "move_to", "args": {"location": "<候选导航路点>"}, "depends_on": []},
    {"step_id": "s2", "action": "vla_skill", "args": {"skill_id": "<候选技能ID>"}, "depends_on": ["s1"]}
  ]
}
```

这只是“拿杯水”的格式示例，尖括号值不能直接下发，不能硬编码一个假拿杯技能。通用验证器要处理所有已获准动作，而不是判断文本里是否含“水”后走专用分支。

模型只提出上述候选；即使提示词列出了合法技能和路点，输出仍是不可信输入。Preflight 用受限快照核对 ID、语义、参数与依赖，生成验证后的 PlanStep.source_refs 和 ValidationRecord，由 TaskService 在所属事务保存。无法一一核实时澄清或阻塞，不能静默替换用户目标。任务输入或模型候选夹带 source_refs、customer_id、approval、validation_result 等保留字段应拒绝；不能把模型填的引用直接当作服务端依据。资料维护 API 可接收来源标识，但仍须核实，不能等同于模型候选的可信执行证据。

验证要求：step_id 唯一、依赖存在且无环；首版完整步骤按链式依赖串行执行；action 必须在允许的机器人动作集合；参数符合统一动作定义；技能 ID 和导航路点属于当前机器人且有资料依据；不得出现模型指定的客户身份或绕过权限的字段。限制计划长度及文本大小的值由配置给出，不在业务代码写魔法数字。

全局词表、服务端允许能力、真实机器人声明、实际就绪条件是四件事。机器人离线或快照缺失应报告 BLOCKED/CAPABILITY_UNKNOWN，不将“没有读到清单”解释成“不支持全部”或“支持全部”。

## 5 模块接口及拒绝规则

以下为接口契约，具体 Python 类型在 domain/models.py 定义。方法不允许偷偷访问其他客户的全局状态。

| 接口 | 输入 → 输出 | 失败规则 |
| --- | --- | --- |
| IdentityPort.resolve_web | 已有 Web 身份凭据 → TrustedContext | 缺失或无效返回 UNAUTHENTICATED；认证源未配置返回 IDENTITY_UNAVAILABLE |
| IdentityPort.resolve_robot | x-robot-token、query robot_id → TrustedContext + RobotBinding | 必须核对凭据允许的客户和机器人标识；不从 hello 创建客户身份 |
| ASRPort.transcribe | 已确认可传递的音频片段 → text + provider_ref | 无配置、超时、空识别结果显式返回错误，不回落 fake |
| InputService.normalize | TrustedContext + 文字／音频 → TaskInput | 原始音频仅在允许范围内处理；文字进入同一后续流程 |
| ResourcesPort.snapshot | TrustedContext + 任务目标／机器人范围 → ContextSnapshot | 缺资料可澄清；缺真实能力或就绪信息则 BLOCKED |
| PlannerPort.plan | goal、已回答问题、ContextSnapshot → CandidatePlan | 格式错误返回 PLANNER_INVALID_OUTPUT，不把自由文本当动作 |
| PlanValidator.validate | CandidatePlan + Snapshot + ActionCatalog → 验证后步骤／服务端 source_refs + ValidationRecord | 纯业务检查，不调用网络或写状态；不支持或缺依据的步骤不进入获准计划 |
| PolicyPort.evaluate | TrustedContext + 操作 + 计划／步骤 + 服务端读取的 ApprovalEvidence → AuthorizationDecision | 按下文组合当前政策与批准证据；历史批准不能覆盖当前 deny 或 unavailable |
| TaskService.submit / answer / decide / cancel / dispatch_next / on_robot_event / recover | 可信调用上下文和对应请求／事件 → TaskView／事务结果 | 唯一任务写用例入口，版本冲突、跨客户访问和重复请求冲突显式拒绝 |
| Preflight.check | 候选／已采纳计划、可信上下文、当前快照、检查阶段 → 检查记录和授权结果 | 复用 PlanValidator 与 PolicyPort；规划采纳和下发前检查分别留证，不自行写状态 |
| Executor.prepare / apply_event / reconcile | 已获准执行单元、锁定记录及证据 → 变化集合／阻塞原因 | 不自行提交，不反调 TaskService；合法变化经 domain，TaskService 原子落库 |
| RobotPort.send | 已持久化 Command → send_observation | 只编码并写入现有连接，不擅自重试或改变计划 |
| RobotPort.events | 认证连接收到的 dlv/0 消息 → RobotEvent | 错误来源、未知指令、类型不合法不能修改执行进度 |
| Store.unit_of_work | TrustedContext → 客户范围内事务接口 | PostgreSQL 不可用时不下发指令，不切换内存“继续执行” |

RobotBinding 至少记录 customer_id、内部 robot_binding_id、允许的 wire_robot_id、命令／语音连接映射、凭据校验引用、服务端能力许可及 isolation_reason、isolation_evidence_refs。隔离时禁止该绑定的新指令，解封规则见第 7.3 节。语音连接 ID 与命令连接 ID 不能靠删除“cmd”后缀猜测为同一机器人，要有服务端绑定。

旧代码中的一个全局 token 不足以证明跨客户机器人归属。可以继续使用相同请求头，但必须有可信的凭据绑定或客户专属部署边界；具体发放方式见 B2。机器人端 member_id 等字段不能绕过身份来源。授权政策未配置时关闭执行入口，而不是默认信任所有已连接设备。

首版接入层用 FastAPI/Pydantic 校验外部请求；domain 使用独立数据对象和检查函数。Pydantic 是数据校验库，不是 Agent 框架，也不证明动作安全。

TaskService.recover 是唯一恢复用例；Executor.reconcile 核对第 9 节证据。恢复与正常反馈共用 domain 归约和同一事务接口，不能再创建 Recovery 的独立状态写入路径。PlannerPort 的实现置于 adapters/llm.py，保留供应商响应解析、超时和有限重试，domain 不解析供应商响应。InputService 不成为第二个计划协调器。

Preflight 涉及外部政策查询时，网络查询在短事务之外完成；进入采纳或下发事务时核实返回证据对应的客户、计划、政策版本、新鲜度与当前生效引用。无法建立有效一致依据时阻塞，不能沿用过期结果。持久化与实际写入门的复查要求仍按第 8 节执行；不能为了统一入口把 ASR、LLM、政策网络等待或机器人发送放进长事务。

### 人工批准与执行前授权怎样组合

decisions API 验证审批者有权批准当前计划后，将 ApprovalEvidence 作为独立证据保存，而不是把任务永久改成“有权限”。该证据与计划版本、客户和批准范围绑定；批准有效期和审批者资格由政策给出，政策未定义有效性时返回 unavailable，不默认永久有效。

PolicyPort.evaluate 先按当前身份、当前政策和操作范围得到 base_result，再计算 effective_result：

- base_result 为 deny 或 unavailable：直接拒绝或阻塞，历史批准不能覆盖。
- base_result 为 allow：无需人工批准，仍须通过其他执行检查。
- base_result 为 require_confirmation：从存储读取同客户、同任务、同计划版本、范围覆盖当前操作、未撤销且未过期的批准证据，并核对其 policy_version 与当前政策一致、审批者仍具备要求的资格；全部满足才产生 effective_result=allow，并记录 approval_refs。
- 没有有效证据时，尚未执行的任务进入 WAITING_APPROVAL；已经执行部分动作的任务进入 BLOCKED/AUTHORIZATION_REQUIRED。后者批准后从保存位置重新检查，不能重做已完成动作。

人工拒绝或任务终态不能因后续政策改变而自动复活。政策版本变化后旧批准失效；是否允许继承批准需另有明确政策，本稿不默认继承。这样既不会每一步反复要求同一批准，也不会把一次批准当永久通行证。

## 6 新增 HTTP API 与用户反馈

以下路径是新云端 API 提案，不是修改 dlv/0。使用 /api/robot-agent 前缀避免直接覆盖旧任务 API。页面实现和旧路由迁移不在本次文档工作的执行范围。

| API | 请求内容 | 成功结果与约束 |
| --- | --- | --- |
| POST /api/robot-agent/tasks | JSON text、可选已登记 robot_binding_id；Idempotency-Key 请求头 | 202 TaskView；完整计划尚未获准前不能产生机器人指令 |
| POST /api/robot-agent/tasks/audio | multipart audio；Idempotency-Key | 完成 ASR 后调用相同 submit，返回 202 TaskView；ASR 失败不创建可执行任务 |
| GET /api/robot-agent/tasks/{id} | 身份凭据 | 200 TaskView；包含 state、reason_code、计划摘要、当前步骤及结果可信度 |
| POST /api/robot-agent/tasks/{id}/clarifications/{cid}/answer | answer、expected_version；Idempotency-Key | 更新同一任务，记录回答后重新生成完整候选计划，不静默改原任务目标 |
| POST /api/robot-agent/tasks/{id}/decisions | plan_id、decision(approve/reject)、expected_version；Idempotency-Key | 只允许有审批权身份提交；计划版本已变化则拒绝旧批准 |
| GET /api/robot-agent/tasks/{id}/events?after={seq} | 身份凭据、游标 | 有界事件列表和 next_seq；授权策略下过滤敏感详情 |
| GET /api/robot-agent/resources/{kind} | kind 为 capabilities 或 environment | 返回客户范围内已生效版本及来源 |
| POST /api/robot-agent/resources/{kind}/versions | base_version、entries、source_refs | 校验及授权后保存新版本；不直接覆盖运行任务引用的旧版本 |
| POST /api/robot-agent/tasks/{id}/cancel | expected_version；Idempotency-Key | 仅在取消策略已配置且允许时受理；返回请求状态，不宣称已物理停止 |

TaskView 至少提供 task_id、version、state、reason_code、clarification、active_plan_id、current_step、result_summary、result_disputed。枚举见第 7 节；结果未知必须可见，不能把内部 UNKNOWN 显示成失败或完成；历史终态结果有争议时必须同时展示争议，不能仅展示原成功结论。

错误统一为 error.code、error.message、request_id；不回传密钥或完整内部异常。401 为未认证，403 为已认证但无权限，404 隐藏其他客户对象，409 用于版本或幂等冲突，422 为输入格式不合法，503 为依赖或必要政策未配置。忙碌时新任务仍返回 202 并按队列处理；API 返回码不能代替任务的业务状态。

### 幂等与澄清关联

幂等表示重试同一请求不会生成第二个任务。Web 写接口以 customer_id、actor_id、操作路径、Idempotency-Key 唯一标识请求；保存请求摘要。同键同内容返回已有结果，同键不同内容返回 409。涉及业务写入时，请求键与写入结果在同一事务中提交。

音频请求可以在重试时再次识别，但事务只准创建一个任务；一旦任务已创建，同键返回该任务，不重新规划执行。原始音频不默认持久保存；不能以隐私未定为由在生产日志中留副本。

dlv/0 语音消息没有已确认的跨连接请求幂等标识，不能要求机器人新增字段，也不能把两次相同文字永久合并。服务端可关联本连接内的语音轮次；跨重连是否重复提交无法仅靠文本保证，必须记录这个限制。

Web 澄清回答使用 clarification_id。机器人语音回答只有在可信身份、服务端绑定的会话及唯一待回答问题能够一致关联时，才作为原任务回答；不明确时不猜关联、不触发执行，返回澄清提示。不能因为同一 robot_id 就将不同用户的话并入同一任务。

### 返回路径

TaskService 在所属状态事务中将追问、拒绝、计划状态和任务结果写成 TaskEvent。Web 端通过 TaskView 和 events 获取；机器人端由输出映射读取同一结果，生成已有 meta/done 等语音轮次消息，不把内部 TaskEvent JSON 直接发到 dlv/0 连接。

result_summary 至少逐步列出 robot_binding_id、动作和参数摘要、command_id、下发观察、接收／终态证据引用、结果及未完成原因；输出按客户和权限脱敏。自然语言从该结构渲染，模型若润色不得添加动作事实。说明示例：“机器人 A：已确认到达桌边（s1/c1，匹配成功反馈）；拿取水杯结果未知（s2/c2，下发期间断线）；任务暂停，尚未收到可靠终态。”A、c1、c2 仅是阅读示例，不代表真实执行。“全部完成”仅在所有必要步骤有合法成功证据时出现。

Web 和机器人端的展示方式可以不同，事实来源和任务 ID 关联必须一致。机器人没有可用的用户反馈方式时，标为联调阻塞，不以“消息已发到 socket”宣称用户已得到结果。

## 7 状态转换

这是新云端的内部状态，不要求机器人发送这些名称。状态变更只能经过 domain/transitions.py 和存储事务；不允许路由或大模型直接写状态。

### 7.1 任务状态

| 当前状态 | 事件／条件 | 下一状态 |
| --- | --- | --- |
| RECEIVED | 输入合法，必要资料可读取 | PLANNING |
| RECEIVED／PLANNING | 缺少会改变结果的信息 | NEEDS_CLARIFICATION |
| NEEDS_CLARIFICATION | 收到可关联且版本匹配的回答 | PLANNING |
| PLANNING | 完整候选计划生成 | VALIDATING |
| VALIDATING | 检查通过且 effective_result=allow | READY |
| VALIDATING | effective_result=require_confirmation | WAITING_APPROVAL |
| WAITING_APPROVAL | 有权身份批准当前计划，重新检查仍通过 | READY |
| READY／QUEUED／BLOCKED | 尚未尝试下发任何指令、无结果未知，仅缺有效批准，effective_result=require_confirmation | WAITING_APPROVAL；重新批准后重新检查，回 READY，不绕过队列 |
| RUNNING／BLOCKED | 已尝试下发，effective_result=require_confirmation | BLOCKED/AUTHORIZATION_REQUIRED；批准后从保存位置重新检查，不重做已完成动作 |
| READY | 获准计划已保存，但执行槽被占用或尚未调度 | QUEUED |
| READY／QUEUED | 获得执行槽及机器人占用，最新权限和执行前检查通过 | RUNNING |
| RUNNING | 所有必要步骤已确认成功 | SUCCEEDED |
| 任何非终态 | 依赖不可用、权限需重新核实或结果未知 | BLOCKED，保存具体 reason_code 和恢复位置 |
| BLOCKED | 条件恢复且没有不明在途动作 | 按保存的恢复位置重新检查，不直接跳过检查；可执行但执行槽被占用则 QUEUED |
| PLANNING／VALIDATING／WAITING_APPROVAL | 明确不支持、检查不通过、拒绝批准或处理失败 | FAILED，附明确原因；不下发动作 |
| RUNNING | 匹配当前指令的明确 failed 终态，后续处理策略未获准 | BLOCKED/ACTION_FAILED，等待获准处理；不能默认重新规划或重试 |
| 非终态 | 取消策略允许且收到取消请求 | CANCEL_REQUESTED，停止后续指令准备 |
| CANCEL_REQUESTED | 不存在可能仍执行的指令，且任务不再继续 | CANCELLED |

FAILED、SUCCEEDED、CANCELLED 为任务终态。FAILED 必须有明确处理依据；RESULT_UNKNOWN 对应 BLOCKED，不映射为 FAILED。客户可见状态通过 TaskView 返回。

恢复位置 resume_phase 属于记录，不是让调用者任意指定的跳转目标：输入／规划阶段回 PLANNING，获准计划尚未开始回 VALIDATING，在途动作回 RUNNING 仅用于接收和核对反馈。未知结果未解决前不能执行 advance。

取消请求不等于物理停止：在途动作可尝试发送既有 cancel，但无取消确认时继续等待证据。若收到 succeeded，记录该动作已经完成，再结束剩余任务；不得向用户称动作已被成功中止。自动取消条件尚未确认，不继承旧“重连即 cancel”。

### 7.2 指令状态与证据

Command.dispatch_state 为 PREPARED、DISPATCHING、SENT；Command.outcome 为 NONE、RUNNING、SUCCEEDED、FAILED、RESULT_UNKNOWN。分开保存下发事实与执行结果，避免“本地发送失败”覆盖机器人已报告的成功。

| 输入或事件 | 云端处理 |
| --- | --- |
| 已保存指令但尚未进入 DISPATCHING | PREPARED；按事务记录可确认尚未尝试下发 |
| 下发前事务提交 | DISPATCHING，记录本次连接代次 |
| WS 写入返回成功 | 写 sent_at／SENT，但不覆盖已到达的执行反馈结果 |
| 匹配的 cmd_status.pending | 记录机器人已报告该指令；不推进步骤 |
| 匹配的 cmd_status.running | outcome=RUNNING，记录机器人接收证据；不推进步骤 |
| 匹配的 cmd_status.succeeded | outcome=SUCCEEDED，记录终态，事务推进一次 |
| 匹配的 cmd_status.failed | outcome=FAILED，进入任务异常处理；不自动再次下发 |
| DISPATCHING／SENT 后断线、超时或写入结果不确定 | outcome=RESULT_UNKNOWN，阻止后续推进 |
| 同一终态重复到达 | 幂等处理，不重复推进 |
| 同一指令出现互相冲突的终态 | 保存冲突证据、隔离执行并报告，不自动覆盖或撤回已执行动作 |
| 未知 cmd_id、错误客户／机器人／旧失效连接来源 | 不修改执行进度；保存受限诊断信息 |

pending/running/终态反馈可作为“机器人报告已处理该指令”的关联证据；不是独立 accepted 确认。没有反馈时 receipt_observed_at 为 null，不能伪造已接收。终态可以先于其他状态到达，不要求机器人补发中间消息。

detail 和 error_code 按现有协议保存为诊断信息，不从自然语言猜测已完成或自动重试条件。反馈含大体积产物时走明确的大小与类型检查，不把任意 base64 写入业务日志。

### 7.3 指令结果的更新优先级

本表优先于第 7.2 节的逐事件说明。处理事件时先锁定对应 Command，按当前状态判断，不能按消息到达顺序直接覆盖 outcome。

| 当前 outcome | 新事件 | 允许的变化 |
| --- | --- | --- |
| NONE | 合法 running／succeeded／failed | 分别转 RUNNING／SUCCEEDED／FAILED；pending 仅补接收观察 |
| RUNNING | pending 或重复 running | 保持 RUNNING，只补观察 |
| NONE／RUNNING，且 dispatch_state 为 DISPATCHING／SENT | 下发不确定、超时、断线 | RESULT_UNKNOWN |
| NONE，且 dispatch_state 为 PREPARED | 断线或等待连接超时 | 保留 PREPARED/NONE；可阻塞任务但不制造未知在途动作，恢复后重新检查再首次下发 |
| RESULT_UNKNOWN | pending 或 running | 保持 RESULT_UNKNOWN，可继续跟踪；不解除后续动作阻塞 |
| RESULT_UNKNOWN | 通过第 7.4 节核验的 succeeded／failed | 转对应终态，按终态事务处理 |
| SUCCEEDED／FAILED | pending、running、发送异常、超时或断线 | 保持原终态，只追加观察，不再次推进 |
| SUCCEEDED／FAILED | 同一终态 | 保持原终态，幂等返回 |
| SUCCEEDED／FAILED | 相反终态 | 保留两份证据，evidence_conflict=true；按下文隔离机器人绑定并标记结果争议，不覆盖原指令终态 |

处理成功终态时还必须检查 Task 状态：CANCEL_REQUESTED 不进入下一动作；已有权限或证据冲突阻塞不因一个成功反馈自动清除。已经执行的动作不能因后来的冲突被“撤销记录”。下发回调只能补 dispatch_state、sent_at 和传输观察，不能将上述终态回退为 RESULT_UNKNOWN。

冲突处理在同一事务内设置 RobotBinding.isolation_reason=EVIDENCE_CONFLICT、保存证据引用，并设置相关 Task.result_disputed=true、追加可见事件。相关任务尚未终结时进入 BLOCKED/EVIDENCE_CONFLICT；已终结则保留历史终态，不复活或抢回原执行槽。若下一任务已使用该绑定，阻止其后续新指令并按保存位置阻塞；已经发出的动作继续接收证据，不宣称被停止。隔离检查与 DISPATCHING 条件更新串行化，实际写入门再次检查；隔离前已开始的网络写入不能靠事务撤销，按可能已下发处理。

隔离持久保存，重启、重连及无关成功反馈均不能自动解除。解封须有核实冲突的证据及获准处理策略（B5）；未定义该策略则保持隔离，不新增默认自动解封行为。

### 7.4 重连后的执行反馈接纳规则

dispatch_generation 是指令最初下发时的连接代次；RobotEvent.connection_generation 是本次反馈来源连接的代次，两者不能要求永远相等。

接纳状态变更须同时满足：反馈来自当前有效认证连接；该连接对应相同 customer_id、robot_binding_id 和允许的 wire_robot_id；cmd_id 在该绑定下有持久化记录且曾进入 DISPATCHING 或 SENT；计划与执行单元关联合法。仅 PREPARED 的指令不能被远端猜测反馈提前完成。

上述条件成立时，新连接可以报告旧连接下发的指令。例如 G1 下发后断线，G2 经过重新认证并上报同一 cmd_id 的终态，可以用于恢复；不需要修改或再次下发原指令。G1 一旦失效，其后到达的反馈仅记录为诊断信息，不能改变进度。机器人是否确实会在 G2 补报，仍属于 B1，不由云端假设。

连接登记更新和反馈应用应串行检查当前代次；迟到反馈关联旧计划的原始指令，不得被当成新计划当前步骤的结果。

## 8 PostgreSQL 记录及事务边界

采用新表前缀 ra_，避免在未迁移评审时改写旧任务表。下表保留 V4 所需逻辑记录，不能仅为减少表数删除恢复或授权依据；数据对象不必各自建表。迁移脚本必须落实对应唯一键、外键和检查约束，不依赖客户端自觉传对 customer_id。

| 表 | 核心字段及约束 |
| --- | --- |
| ra_robot_bindings | customer_id、binding_id、允许的 wire_robot_id／语音映射、凭据校验引用、allowed_capabilities、enabled、isolation_reason、isolation_evidence_refs；认证凭据不以明文返回或落日志 |
| ra_resource_versions | customer_id、kind、resource_id、revision、payload、source_refs、created_by、created_at；版本不可覆盖，记录生效版本指针 |
| ra_tasks | customer_id、task_id、actor、goal_text、state、reason_code、resume_phase、active_plan_id、clarification、queue_seq、queued_at、result_disputed、version、timestamps |
| ra_plans | customer_id、plan_id、task_id、revision、完整 steps、snapshot_refs、validation_id；(customer_id, task_id, revision) 唯一 |
| ra_units | customer_id、unit_id、task_id、plan_id、binding_id、state、next_step_id、version |
| ra_commands | customer_id、command_id、unit_id、step_id、attempt、不可变 wire 内容、下发与结果字段；(customer_id, unit_id, plan_id, step_id, attempt) 唯一；command_id 唯一 |
| ra_decisions | customer_id、decision_id、task_id、plan_id、step_id、kind(validation/authorization/approval)、result、rule_or_policy_version、evidence_refs、created_at；approval 类型还需保存 ApprovalEvidence 字段 |
| ra_robot_events | customer_id、event_id、binding_id、connection_generation、cmd_id、received_at、受限 payload；重复消息不能导致重复状态推进 |
| ra_task_events | customer_id、task_id、seq、task_version、event_type、public_payload；(customer_id, task_id, seq) 唯一 |
| ra_request_keys | customer_id、actor_id、operation、key、request_digest、result_ref；前四项唯一 |
| ra_execution_guard | 部署执行槽标识、customer_id、task_id、unit_id、binding_id、owner_run_id、state；用于首版唯一活动任务及机器人占用 |
| ra_runtime_owner | deployment_id 主键、owner_host_id、owner_run_id、state、started_at；只保存部署运行所有权，不承载客户业务数据 |

父子记录使用带 customer_id 的复合关联约束；更新语句必须包含 customer_id 和预期 version。跨客户关联在存储层再次拒绝。错误中的对象存在性不能泄露给其他客户。

执行快照既引用资料版本，也保存当时的机器人技能与就绪观察，避免只保存指向可变内存字典的引用。所有状态与决策记录的时间语义为 UTC；任务事件序号在任务事务中递增。

### 必须原子提交的四组变更

1. **受理任务**：请求幂等键、Task 及首个 TaskEvent 一起提交。
2. **采纳计划**：不可变 Plan、ValidationRecord、AuthorizationDecision、执行安排和 Task 状态一起提交。人工批准绑定具体计划版本，不批准一个可随意替换的 task_id。
3. **准备下发**：锁定执行单元及执行占用，复查当前计划、权限和执行条件，保存检查依据及 Command，提交后才允许 WS 写入。
4. **处理终态**：保存 RobotEvent、检查指令归属及当前版本、更新 Command、推进 ExecutionUnit、汇总 Task、追加 TaskEvent，一起提交。重复终态命中已处理状态时不再次推进。

数据库事务不能覆盖物理动作。不能在事务重试时重跑 WS 写入，也不能把模型、ASR 或 WS 网络等待放在长事务中。

### 下发伪代码

```text
advance(unit):
  在事务中核实客户、计划版本、唯一执行权、机器人占用和授权
  若当前步骤已有活动指令或结果未知：返回当前状态，不创建第二条
  保存 PREPARED 指令及执行前检查结果
  再以条件更新标记 DISPATCHING，绑定当前连接代次
  提交后调用 RobotPort.send，最多一次
  WS 返回成功：只补下发观察，不回退已收到的终态
  WS 写入异常或进程在该窗口中断：结果未知，进入核对
```

准备和标记 DISPATCHING 之间若有等待，标记时必须重新核实授权、资料有效性和执行权，不能使用过期检查放行。运行实例重启时，PREPARED 可以重新检查后首次下发；DISPATCHING／SENT 必须先核对，不能因没有 sent_at 就推断未发送。

生产存储不能降级为内存继续控制机器人；内存实现只可在测试中注入。检查、授权和执行记录缺失时均禁止下发。

### PostgreSQL 任务队列

用户已确认新任务排队并稍后自动执行。队列是 ra_tasks 中持久化的业务状态，不引入独立消息队列中间件。提交新任务返回已有的 202 TaskView，不因机器人正在执行任务就返回 TASK_BUSY；可先完成输入处理、完整计划生成和授权，再置为 QUEUED。

本稿提出按 queue_seq 先入先出选择已获准、可执行任务；队列顺序、任务有效期和失效任务处理仍需评审，不能声称用户已经批准 FIFO 或永久有效。待澄清、待批准和依赖不可用任务不参与自动下发。QUEUED 仅表示等待执行，不表示机器人已接收。

单实例调度循环在启动、任务终结和有界周期扫描时检查数据库。选择任务与取得唯一执行槽必须在同一事务中完成；进程内唤醒只负责减少等待，数据库记录才是队列依据。没有可用槽时继续保留 QUEUED，不丢任务，不占用第二个执行槽。

任务开始前重新检查原发起方当前权限、计划版本、能力、就绪证据新鲜度及绑定隔离状态。根据 effective_result 按第 7.1 节分流：尚未尝试下发且仅缺有效批准时转 WAITING_APPROVAL；已尝试下发而需重新批准时转 BLOCKED/AUTHORIZATION_REQUIRED；其他条件变化转 BLOCKED 并记录原因。不静默换目标、换机器人或使用旧批准。当前任务有结果未知或可能仍在执行的动作时，不释放执行槽给下一任务。取消排队任务不发送机器人 cancel；它尚未开始物理执行。

重启后先核对旧在途任务，再调度队列。已入队但未下发的任务不能被当成在途动作，也不能绕过自动恢复阶段直接启动。

### 首版唯一执行权的实现方案

运行所有权与任务占用不同：ra_runtime_owner 决定哪个进程可以下发，ra_execution_guard 决定当前哪个任务和机器人占用执行槽。进程重启不自动清空任务占用。

双锁和两种记录不是多机器人调度或自动接管平台：主机锁控制同主机进程重叠，数据库锁控制部署执行权，持久任务占用防止未知在途动作被当成空闲。V5 保留 V4 的这一安全提案，封装在 Runtime／RobotPort 写入门，不把锁检查散布为多套业务状态机。只有用部署与故障场景证明等效保障后才能替换；“首期单实例”是必须执行的约束，不能代替保证单实例的机制。

本稿提出以下单主机运行机制作为第 11 节部署方案的一部分，仍需结合 B6 确认部署环境：

1. 每个 deployment_id 固定一个获准主机。进程先取得本地主机持久目录中的排他进程锁，全生命周期持有，不删除或替换锁文件。macOS/Linux 可用 Python 标准库 fcntl.flock；其他平台必须提供并验证等效实现后才能启用执行。
2. 使用独立、不可自动重连回池的 PostgreSQL 连接，取得 deployment_id 对应的会话级 advisory lock。锁键用固定配置的整数，不用随进程变化的 hash()。未取得任一锁的第二进程不得注册为下发者。
3. 在事务中检查 ra_runtime_owner。相同获准主机在取得两把锁后，可以创建新的 owner_run_id 并进入恢复；不同主机不能因数据库锁已释放就自动接管，必须先确认原主机已停止且不会继续发送，再通过受控部署变更启用。
4. 每次准备指令及每次实际 WS 写入前，检查本地禁止发送标志、持锁连接健康和数据库 owner_run_id。持锁连接丢失、所有权不符或存储故障时，本进程立即进入不可逆的禁止发送状态，不在原进程中重连后自动恢复下发；正常重启后重新取得锁并进行恢复核实。
5. 正常关闭先禁止新下发，收尾当前 WS 写入并记录能确定的结果，关闭命令连接，再释放数据库锁和本地主机进程锁。异常退出造成的在途结果按第 9 节处理。

数据库会话锁在连接结束时释放，因此它单独不能防止仍持有机器人连接的旧进程继续发送。本地主机进程锁和禁止跨主机自动接管用于弥补这个边界；这是首版固定主机、单实例约束下的方案，不是跨机器的热备保障。锁丢失前后已发出的指令仍可能在机器人执行，不能以新进程获得锁为由重发。

不允许绕过 RobotPort 写入门直接操作 WS；不得通过改 deployment_id、owner_host_id 或另建锁文件绕过互斥。发布工具必须验证固定主机及唯一运行进程。参考 [PostgreSQL 会话级锁语义](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS) 与 [flock 语义](https://man7.org/linux/man-pages/man2/flock.2.html)。

## 9 断线和重启恢复

1. 停止为受影响机器人准备新指令；保持执行槽占用，不把连接断开当作机器人空闲。
2. 启动恢复前，确认旧运行实例已结束。首版不实现依靠超时自动争抢执行权的多实例热备。
3. 扫描本部署范围内未结束记录，逐客户加载，重新核验凭据、机器人绑定和当前权限。
4. 通过已有 hello、cmd_status、readiness、telemetry 更新证据。恢复流程不发送 dlv/1 或自定义查询帧。
5. 已有持久成功终态可避免重做；重新收到匹配终态时按正常终态事务处理；匹配 busy_cmd_id 只能说明有在途指令，不能证明成功，继续跟踪。
6. 无法关联结果时保持 RESULT_UNKNOWN／BLOCKED，保留占用，不重发、不推进依赖步骤。
7. 证据解决后复查计划、权限、能力和机器人占用，再按记录恢复；不因重连就自动 cancel。

**能力边界：**当前 Agent 侧资料不能证明 dlv/0 机器人会补报断线期间的全部历史结果，也未发现已接入的可靠历史查询。因而“任何断线都能自动恢复完成”不能保证。自动继续的前提是取得可靠证据；缺证据保持未知是安全要求，不是已经满足全部恢复目标。是否可取得这些证据列为 B1 真机验收阻塞，禁止改协议绕过。

明确收到 failed 也不意味着物理效果可以回滚。是否重新规划、换机器人或再次执行由获准策略决定；未知状态尤其不能交给大模型猜测。

## 10 能力和环境资料维护

维护流程为：已认证维护者 → resources 入口 → 权限检查 → 格式及来源检查 → 保存新版本 → 更新生效指针 → 新任务使用新快照。

资料至少覆盖：客户归属、机器人绑定、动作参数约束、机器人技能 ID 的已核实含义、环境实体与导航路点映射。业务名称可以由本服务维护，但不能把自定义名称当作机器人已支持的新 wire skill。

运行计划固定引用原版本；下发前同时检查当前授权和实际可用条件。更新后发生冲突时阻塞并重新核实，不静默替换正在执行的指令参数。资料缺失与大模型不理解任务是不同原因，错误码应区分。

具体维护人员、修改审批以及生效政策仍需确认。未获准的资料写入返回拒绝；禁止用默认演示桌子、杯子、技能目录充当生产资料。

## 11 配置与运行

首版已确认 PostgreSQL、单运行实例、无消息队列中间件；任务队列按第 8 节保存在 PostgreSQL 中。客户共享或专属部署、存储物理隔离形式未定；访问隔离不因此推迟。

配置必须分组并启动检查：
- 存储连接与部署执行槽标识；
- Web 身份来源、机器人凭据绑定及授权政策引用；
- ASR API 提供方、凭据引用、允许外传的数据范围；
- 大模型 API 提供方及模型名；
- 输入大小、计划长度、网络调用和指令等待超时；
- 日志脱敏、录音与资料保留政策。

敏感配置不写入文档示例、不返回客户端、不进入大模型上下文。参数尚未确定时，不用旧代码默认值冒充批准值；部署必须显式提供经评审的配置。API 不可用只影响相应能力，ASR 故障不能切断机器人执行反馈通道。

启动顺序：配置检查 → 存储与身份绑定 → 载入动作契约 → 建立未结束任务隔离状态 → 开放认证后的 WS 连接及任务受理（可处理输入、规划和排队）→ 核对并恢复有证据的任务 → 允许执行槽空闲时调度新任务。旧在途结果未知不阻止新任务入队，但阻止新指令下发。关闭时先停新下发、持久化状态，再关闭连接；不声明关闭连接等于机器人停机。

延迟分别记录：输入处理耗时、规划耗时、首指令下发耗时、步骤间衔接耗时。尚无用户批准的硬时限，不写未经测量的性能承诺。

## 12 开发顺序和验收测试

每个阶段只修改获准范围。优先穿过一条最小完整任务链，再补齐持久化与通信保障；不是先构建全部模块，再到末期才验证流程。以下是实施顺序，不是本次已执行的工作。P0 的模拟和内存替身只存在隔离测试中，不能作为生产降级或首期交付；首期完成仍需满足全部 T01—T26 及适用前置条件。

| 阶段 | 实现范围 | 进入下一阶段的条件 |
| --- | --- | --- |
| P0 | 隔离测试中实现 submit → 候选 → Preflight → 计划采纳 → 一个指令 → 反馈 → 用户结果；使用标识清楚的 Planner／Robot／Store 替身 | 验证主线、职责调用及拒绝时零下发；不宣称真实 LLM、数据库、语音或真机通过 |
| P1 | 扩展 domain 状态、TaskService／Executor 共同反馈归约与资料边界；PostgreSQL 事务、幂等、客户隔离 | T01—T07 中实际相关场景通过；T01 用已识别文字验证入口，实际 ASR 在 P4 补验 |
| P2 | dlv/0 编解码、认证绑定、连接代次、真实 Store 与协议模拟器联动 | T08—T12 通过；协议模拟不连接真机，不要求新增机器人字段 |
| P3 | 持久队列、唯一执行权、崩溃窗口、恢复及批准再检查 | T13—T18、T23 后端队列场景通过；内存测试不足以证明双进程／数据库断连保障 |
| P4 | 真实模型及外部 ASR、HTTP API、资料维护、文字／语音共享入口、最小客户端和兼容输出 | 补齐 T01、T19—T23；演练一条提交到结果的客户端组合链路；相关 B2—B7 落实 |
| P5 | 受控真机联调与部署核验 | B1—B7 中首版相关资料／政策落实并获准联调；T24—T26 通过，全量复核 T01—T26 的真实覆盖与限制 |

建议在 tests/robot_agent 中为以下场景建立可自动执行的测试。PostgreSQL 集成测试使用隔离测试库；真机联调需明确授权对象，不能把生产凭据用于随意测试。

| 测试 | 场景与预期 |
| --- | --- |
| T01 | 文字与 ASR 文字进入同一任务业务入口 |
| T02 | 完整计划尚未生成、检查失败或未获准时，RobotPort.send 调用次数为 0 |
| T03 | 未知动作、遗留 grasp 新计划、虚构 skill_id／路点、循环依赖被拒绝；能力快照缺失为 BLOCKED 而非默认支持 |
| T04 | 客户、用户、动作、指令及执行单元概念分离；非法状态变化不能直接写入 |
| T05 | 同幂等键同请求得到同一 task_id；同键不同内容 409 |
| T06 | 候选计划、检查或授权记录保存失败时不下发；外键禁止跨客户关联 |
| T07 | 两个并发推进或出队请求只产生一条当前步骤指令；只存在一个首版活动任务执行槽 |
| T08 | 现有路径、robot_id、x-robot-token、ready、hello、command 和 cmd_status 格式不变 |
| T09 | hello 身份与绑定不符、其他客户 cmd_id、未知命令及失效连接反馈不改变状态 |
| T10 | pending/running 不推进，succeeded 推进一步，failed 不自动重试；跳过中间状态直接终态仍可正确处理 |
| T11 | succeeded 后迟到 running、发送异常、超时或断线均不回退结果；反馈早于 send 返回同样不回退；重复终态不重复推进；旧任务结束、下一任务启动后收到冲突终态，旧任务保留终态并显示争议，绑定隔离阻止后续下发但不宣称已停止在途动作 |
| T12 | cancel 发送成功不宣称停止；interrupt 和语音 stop 不被当作物理急停；不发送 dlv/1 或自定义查询 |
| T13 | 在 PREPARED、DISPATCHING、SENT、反馈落库前后分别中断，恢复行为符合第 8、9 节；PREPARED 后断线仍保留未尝试下发事实，不产生未知在途动作 |
| T14 | busy_cmd_id、busy=false 或位置快照不能被当作完成证据；未知结果不释放执行占用；持续遥测与快照重建不能延长旧就绪证据有效期，重连必须取得当前连接的必要就绪证据 |
| T15 | G1 下发后 G2 当前认证连接合法补报终态可用于恢复，G1 失效连接迟到消息不能推进；没有补报证据则保持 BLOCKED/RESULT_UNKNOWN |
| T16 | require_confirmation 配合有效当前计划批准可执行，不重复索要批准；批准撤销、过期、政策版本变化、当前 deny/unavailable 或能力冲突时禁止下发；排队任务仅因批准失效回 WAITING_APPROVAL，重新批准后重查并入队；已下发任务需批准则 BLOCKED/AUTHORIZATION_REQUIRED，恢复不重做已完成步骤 |
| T17 | 计划版本变更后旧批准不可用，旧反馈只能关联原指令 |
| T18 | 替换连接后旧连接清理不删除新连接；第二进程拿不到进程锁或数据库锁时不能下发；持锁连接丢失后原进程禁止发送；新主机不能自动接管；重启不清空未知动作的任务占用 |
| T19 | ASR、大模型、政策或存储未配置时显式拒绝相关操作，不虚构成功 |
| T20 | audio／segment 输入、meta／done 及 idle/error 收尾与旧客户端兼容；每条 WS 连接仅一个读取循环 |
| T21 | 未确认说话人不能仅靠 member_id 获权；环境音外传和录音保留受明确政策限制 |
| T22 | 追问、回答、计划批准、进度、结果均可关联同一任务；跨用户回答不得串任务 |
| T23 | 执行期间新任务可持久入队；当前任务安全终结后自动调度，重启不丢队列；出队重新检查权限与条件；未知在途结果不释放执行槽 |
| T24 | 实际技能目录和环境映射支持通用任务流程；拿杯水例子仅执行已确认的两个业务步骤 |
| T25 | 另一种已支持任务也通过同一流程，不依赖拿杯水专用分支 |
| T26 | 客户隔离、真实机器人动作与恢复证据经过授权联调；模拟测试不代替真机验收 |

接入新包时同时检查 pyproject.toml 的包发现配置、测试路径及依赖检查规则。文档规定的文件尚未创建，不能现在执行不存在的测试目录并报告通过。建议将上述测试编号用于开发任务和验收记录，记录每项实际执行证据。

### 架构变更验收场景

| 编号 | 变更／故障 | 预期边界与实际验证 |
| --- | --- | --- |
| A01 | 更换 LLM 供应商 | 更换适配器／配置，TaskService、Command 归约不改；运行相同候选契约和 T02—T03，拒绝伪造 source_refs 与权限字段 |
| A02 | 增加文字之外的语音入口 | 转写后进入同一 submit；输入用例不复制授权与状态；T01、T20—T22 与客户端闭环验证 |
| A03 | 更换 dlv/0 传输实现而保持语义 | domain 不感知 SDK；新适配器跑同一编解码、写入门和反馈契约；T08—T15；改变协议语义另行评审 |
| A04 | 下发期间断线、终态重复、下一任务后到达冲突 | 共用同一 Executor 归约和 TaskService 事务；T11、T13—T15 验证结果未知、一次推进和隔离，不设置第二套恢复状态 |
| A05 | 数据库断连、第二进程启动 | T06、T18 验证无记录不下发、双锁及旧进程禁止发送；不能用正常启动成功代替 |
| A06 | 注册另一个机器人绑定，仍保持首期单执行槽 | 隔离测试验证身份、能力和历史指令归属，现有任务记录仍可读取；不声称已实现并发、交接或协同策略 |
| A07 | 展示事实结果、通知丢失后重查 | TaskView／TaskEvent 来自同一持久记录；重复回读一致、客户隔离有效、未知不改写为完成；对应 T11、T22、T26 |

A01—A07 是 V5 新增的验证要求，当前均未执行。它们衡量变化影响与故障行为，不以改动文件个数或主观分数判定架构优劣。

## 13 待确认决策与真机前置条件

| 编号 | 需要确认或取得的内容 | 不解决时的明确行为 |
| --- | --- | --- |
| B1 | 当前机器人实际 hello、brain_skills、waypoint_names、终态反馈样例；断线后可补报哪些指令证据 | 无依据的技能／目标不执行；不承诺无条件自动恢复，不改 dlv/0 补功能 |
| B2 | Web 用户和机器人凭据如何绑定客户；机器人端任务的授权主体、跨客户管理权限 | 无法建立可信客户或执行身份则拒绝，不照搬全局 token 或自报 member_id |
| B3 | 已确认排队并自动执行；仍需确认队列顺序、任务有效期、失效处理及执行前通知方式 | 本稿提出 FIFO 方案供评审；不得将排队视为永久有效授权；不能恢复为忙碌拒绝策略 |
| B4 | 流式或整段 ASR、外传与录音保留规则、语音播报及旧 HTTP 降级入口兼容要求 | 音频处理或输出政策未确定的路径不能通过生产验收，不能伪造语音交互完成 |
| B5 | ASR／大模型实际提供方配置，执行授权及异常处理策略，资料维护权限 | 相应 Port 返回 unavailable／拒绝；可以写接口与测试，但不能声称生产功能完成 |
| B6 | 客户共享或专属部署、具体数据隔离与保留要求、配置上限与响应验收目标 | 不擅自选择部署或宣称性能指标；部署前必须明确 |
| B7 | dlv/0 无跨连接输入幂等键时，用户重复提交的产品处理方式 | 不宣称机器人端输入具备 Web 同等的跨重连幂等保障，不仅凭文字相同自动合并任务 |
| B8 | 未来多机器人并发数、资源占用、交接证据、部分失败与换机策略 | 只保留执行单元与依赖边界，首版不实现或宣传多机器人协同完成 |

R3、R4 和新任务排队行为已确认，不再列为待定；PostgreSQL、单运行实例、无消息队列中间件也已确认。上述问题中，产品权限和体验由用户决定；协议事实由开发者核实资料或授权联调；接口实现与数据库方案由架构设计给出并接受评审。

按影响安排解决时间，不把不同类型决策混成全部阻止开发：
- **相关行为启用前：**B2 身份与权限主体、B3 队列政策、B4 音频与输出政策、B5 授权／提供方／维护规则、B7 重复输入政策；可先开发明确接口和隔离测试，不能猜测产品行为。
- **真机与部署验收前：**B1 实际协议证据、B6 部署和数据隔离条件，以及上述政策与配置的实际落实。双锁方案是受 B6 约束的技术提案，不因写入 V5 自动成为已批准的部署事实。
- **未来阶段：**B8 多机器人并发、选机、分工、等待交接、资源竞争、换机和部分失败策略；首期只预留已有的绑定、执行单元与依赖，不规划或实现这些算法。首期不实现跨主机自动接管和热备。

## 14 与现有仓库的集成边界

旧项目作为协议、供应商适配和测试经验的参考，新应用不得依赖旧任务服务、记忆、组织业务或启动装配。可以在独立项目或本仓库的独立应用包实现；目录选择不是优雅证明。最终落点、协议定义如何单源维护及发布方式另行确认，不自动创建仓库。若提取协议定义，记录来源版本并做契约漂移检查，不能无管理地复制出两套词表。

启用新执行链路前，要单独评审接入注册、旧执行器停用、身份绑定和旧任务处理方案；同一 WS 路径只能有一个实际处理器，同一执行单元只能有一个推进者。与旧平台协作优先使用明确接口，不能共写旧任务表或直接访问旧驱动私有状态。

不自动迁移旧未结束任务，不把旧 timeout=failed 或重连即 cancel 的记录当新恢复证据。新表迁移要可回滚，但回滚软件不能撤销物理动作；切换前要核实机器人执行状态。旧业务删除和生产变更不在本次授权范围。

## 15 依据与文档验收

代码核实位置：
- README.md、AGENT.md、CLAUDE.md：当前项目边界、架构开发原则与现有规范；现有行为不自动成为新需求；
- src/domains/robots/dlv_schema.py：协议版本、动作、能力和消息声明；
- src/domains/robots/ws_registry.py、ws_driver.py：hello 解析、command 编码、状态与取消语义；
- src/api/routes/voice_ws.py、src/domains/voice/turn_pipeline.py：WS 路径、认证请求头、语音消息及半双工收尾；
- src/domains/tasks/planner.py、grounding.py、service.py：新计划不使用遗留机械臂动作、实际技能和路点来源；
- src/domains/voice/asr.py、_doubao_asr.py、_qwen_asr.py：已有 ASR 选项与不能当作真实识别的 fake 分支；
- pyproject.toml、docker-compose.yml：现有 FastAPI、Pydantic、asyncpg 与 PostgreSQL 依赖；
- AGENT.md 第 8.1 节：职责、信息隐藏、状态事实、适度抽象与场景验证的审查依据。

本稿只规定兼容接口和建议的新实现，不将旧源码全部视为新系统需求。未取得的部署登记或机器人端资料不能作为已读证据；本稿不能证明当前生产协议上报行为。

文档验收要求：一个没有本次聊天上下文的读者，应能复述目标、确认事项和边界，指出代码位置、接口和唯一写入负责人，追踪一次正常任务及一次丢失反馈，明确哪些可开始实现、哪些受 B1—B8 限制，并指出 T01—T26、A01—A07 的实际证据要求。读懂文档不等于运行验收通过，正式开发前仍需用户评审内部技术方案。

## 16 V5 为什么更清晰，以及如何质疑它

### 16.1 结论的准确说法

V5 的主张是：**在保持 V4 同等需求、协议兼容和安全恢复约束的前提下，明确决定权、复用状态规则、隔离供应商变化，让开发者更容易解释和验证系统。** 这是可检验的设计改进，不是“V5 已经完美优雅”的保证。V4 本来已有分层、权限和故障证据规则；同名 resources 文件和较多数据对象不证明 V4 重复或错误。V5 补足的是职责交接、写入入口与可验证边界，不能把规格补充说成修复了实际运行缺陷。

### 16.2 一条正常链路与一条故障链路

正常任务（假设资料、权限和绑定已核实，供理解流程，不是物理成功证明）：
1. Web 文字或机器人语音经过输入适配，得到可信上下文与 TaskInput，都调用 TaskService.submit；它先在受理事务保存请求键、Task 和首个 TaskEvent，同键重试不另建任务。
2. TaskService 读取快照、调用 PlannerPort，取得候选步骤；Preflight 用相同的验证／授权规则生成服务端依据。
3. TaskService 在采纳事务保存完整不可变计划、检查／授权记录、TaskEvent 和执行安排；需要澄清／批准时没有指令。批准由单独的 decide 用例核实审批者、计划版本和范围后保存，再检查有效授权，不虚构尚未取得的批准。
4. dispatch_next 原子取得执行槽，复查当前条件；Executor.prepare 产生指令变化，由 TaskService 保存 PREPARED 和后续 DISPATCHING。提交后由 Executor 调 RobotPort.send 经过唯一写入门发送一次。
5. 认证读取循环收到 cmd_status 后调用 on_robot_event。Executor.apply_event 核验原指令，domain 归约结果；TaskService 在同一事务保存 RobotEvent、Command、单元游标、Task 和 TaskEvent。
6. 第一步有可靠成功证据才允许准备下一步。用户视图和回复读取这些持久事实，不依赖聊天模型自行宣布任务完成。

下发期间断线：Command 已进入 DISPATCHING 或 SENT，但没有可靠终态，反馈／故障用例按同一归约规则记 RESULT_UNKNOWN，保留执行槽；runtime 启动后只调用 TaskService.recover，Executor.reconcile 检查已有证据。若当前认证新连接补报可核验终态，仍经同一反馈事务推进；否则继续阻塞。没有独立 Recovery 状态机，没有按超时重发，没有因位置看起来正确就宣布完成。

### 16.3 与架构原则的对照和代价

| 原则／V4 需要澄清处 | V5 具体决定 | 收益与代价 | 如何反驳或验证 |
| --- | --- | --- | --- |
| 高内聚：协调、执行、恢复之间的状态交接未完全写清 | TaskService 是任务用例入口；Executor 提供生命周期变化；domain 决定合法变化 | 明确谁组织一次原子更新；代价是入口需认真分解，不能把全部算法堆在 TaskService | 若任务入口开始解析协议、供应商响应或每种技能，违反第 3 节；T10—T18 验证原子推进 |
| 信息隐藏：流程分层本身不足以隔离变化 | 只在真正外部依赖处用 Port，内部直接协作；协议与 SDK 留在适配器 | 替换依赖不扩散至核心；代价是维护契约及适配测试 | A01、A03 若需要改业务状态规则且没有语义变化，说明边界失败 |
| 统一事实：协调器和执行器同时涉及状态与结果 | 变化集合在唯一用例事务落库；TaskEvent 为投影，不建设第二套执行状态 | 重复反馈、通知丢失不造成两套结果；代价是定义事务及游标 | T11、A04、A07；若回调分别提交 Command 与 Task，中断后不一致，设计未落实 |
| 模型隔离：候选格式中有 source_refs，虽要求核验仍易混淆 | 模型仅提出 action／args／depends_on，服务端接地后生成 source_refs | 消除“模型引用就是证据”的误用；代价是维护真实能力与环境资料 | A01 伪造引用、技能、身份均不能产生可执行计划；服务端资料本身也要核实来源 |
| 适度抽象：把文件、对象、接口、表混为一谈易误判复杂度 | 明确逻辑对象可嵌套／合表；执行单元保持首期单行；不引入通用注册平台 | 结构完整而不机械分服务；代价是必要实体和事务仍然不少 | 每个对象都能对应当前证据／恢复／权限需求；不能以少表为理由删 T06、T16、T17 |
| 故障一致性：单独 recovery 文件可能被实现成旁路 | recover 是任务用例，reconcile 与正常反馈共用 Executor 和 domain | 减少旁路规则；代价是所有故障仍要逐项覆盖 | T13—T15、A04；辅助文件数量无关，是否有独立状态决定权才关键 |
| 需求相称：未来规模和首期部署不能混写 | B8 不实现多机器人算法，B6 约束首期双锁部署；不开发热备／自动接管 | 首期成本集中在已确认闭环；代价是未知结果可持续阻塞队列，跨机接管需要运维 | T18、T23、A05—A06；系统不能宣称高可用或多机协同完成 |
| 早验证：按层完成后才拼接，反馈较晚 | P0 隔离纵向闭环，逐阶段换成真实存储／模型／协议并保留全部验收 | 早发现职责接线问题；代价是明确标识替身并逐阶段补验 | P0 不能算首期完成；P5 核对全部 T 与 A 的证据，真机不能被模拟替代 |

### 16.4 常见质疑与可回答的边界

- **“TaskService 又变成大类怎么办？”** 它只编排用例和事务，细节归 domain、Preflight、Executor、Resources、adapters；内部可拆用例文件，不能再建相互回调的状态写入者。是否成立看调用与依赖，不看类名或行数。
- **“API 和 application 都有 resources，不是重复吗？”** 前者负责接入，后者负责业务；只要没有重复的维护／授权规则，就是合理分层。文件少不是目标。
- **“为什么保留这么多状态、记录和两把锁？”** 指令、任务、批准和证据表达不同事实；主机锁与数据库锁面对不同故障。能以更小实现达到同等保障可以替换，但必须给出等效性场景和证据。PostgreSQL 会话锁在会话结束后释放，不能单独阻止还活着且持有机器人连接的旧进程发送；因此不能直接删主机约束。
- **“为什么不直接上 ReAct／多个 Agent？”** 当前需求是先完整规划、检查后执行；通用自主循环没有必要被放进物理执行核心。模型可以再次参与规划，但必须生成新版本、重新检查和获准，不能改变在途指令事实。
- **“多机器人不是未来要做吗，为什么只有一个执行单元？”** 单个执行单元是首期可验证的基线，绑定与依赖保留扩展点；数据边界不证明选机、交接、并发和资源算法已经解决，B8 后续另行设计。
- **“保留原协议能保证自动恢复吗？”** 不能。dlv/0 没有已核实的可靠历史查询，缺补报证据会持续阻塞。这是公开限制；若业务不能接受，应先讨论协议能力或产品约束，不能让模型猜测补齐。
- **“独立项目是不是天然优雅？”** 不是。仓库只提供管理边界；独立启动、依赖、数据所有权及 A01—A07 的结果才证明解耦，不能复制旧 TaskService 后改名交付。
- **“为什么不把 V5 称为正式证明？”** 目前仅有规格与静态核对，没有实现、性能数据、正式 ATAM 或真机结果。可辩护的是设计理由，运行收益仍待验证；失败场景应能推翻不成立的设计，而不是预设结论。

### 16.5 依据和对外讲解方式

上述决定是对 [AGENT.md 架构开发原则](../../../AGENT.md) 的具体应用。理论依据为 [IEEE SWEBOK 的设计原则](https://ieeecs-media.computer.org/media/education/swebok/swebok-v4.pdf)（关注点分离、信息隐藏、接口与实现分离、耦合／内聚）、[Parnas 的模块划分论文](https://www.cs.lafayette.edu/~gexia/cs301/resources/parnas.html)（隐藏容易变化的决定）、[SEI ATAM](https://www.sei.cmu.edu/library/architecture-tradeoff-analysis-method-collection/)（从业务目标和具体场景分析权衡）及 [YAGNI](https://martinfowler.com/bliki/Yagni.html)（不提前实现推测能力，但保持可修改性）。这些依据支持评价方法，不替本项目提供通过结论。

向别人介绍时可以说：“V5 让模型负责候选规划，让同一套代码负责检查和事实推进；TaskService 把指令与任务变更放在一次事务里，外部协议和供应商通过适配器隔离。它保留物理执行必要的未知与恢复证据，推迟未来协同算法。我们将用换模型、换传输、重复反馈和断线恢复场景验证这些边界，目前这是可检验的设计方案。”

请同时说明代价：需要资料维护与数据库事务，首期固定主机单实例，没有高可用接管，结果未知时不保证任务自动完成。避免说“减少几份文件就是优雅”“已经消灭所有耦合”或“任何故障都能自动恢复”。
