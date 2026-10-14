# 业务数据与部署配置：完整清单和开发边界

日期：2026-10-14。源码核对基线：`ro-agent@60ac3e206d01a0a04d1399cb167b66c500d326a5`。

本次只更新技术文档，未实现账号登录、动态设备管理或动态授权。本文是数据分类、权威来源与生效规则的唯一说明；总规格、部署说明和实施计划引用本文，不各自维护另一份规则。

## 1 先理解要改什么

普通用户注册、停用成员、登记机器人、调整权限是使用系统时的业务操作，不应依赖修改部署文件和重启。数据库连接、运行主机、供应商连接和平台技术上限是部署参数，仍由运维管理。协议格式与合法状态变化是程序规则，不作为随意修改的业务开关。

开发顺序见[专项实施计划](superpowers/plans/2026-10-14-data-configuration-boundaries.md)：先明确字段归属与迁移，再改身份、政策和登记的读取入口，最后验证在线变更与重启恢复。实施代码需另行批准；本次授权只覆盖文档。

### 分类词汇

| 分类 | 大白话 | 权威来源与更新者 |
| --- | --- | --- |
| 业务数据 | 谁在使用、有什么机器人、允许做什么、任务发生了什么 | PostgreSQL；授权管理用例或任务、资料、反馈用例更新 |
| 部署配置 | 这个服务连接哪里、运行在哪里、技术上限是多少 | 非敏感配置文件与环境变量；运维经评审更新并受控重启 |
| 系统持久记录 | 哪个进程持有执行权、安装了哪些数据库迁移 | PostgreSQL；运行所有权和迁移组件更新，不开放给客户修改 |
| 临时运行状态 | 正在连接的 socket、锁句柄、调用中的音频和异步任务 | 进程内存；断线或退出后重建，不能代替持久执行证据 |
| 程序规则 | 协议格式、参数校验、合法状态、结果未知的处理 | 代码与契约测试；通过代码评审和发布更新 |

分类不只看名字或改动频率。先问“谁改、何时生效、是否需要客户关联和历史证据、启动前是否必须知道”。例如审批有效期虽然是参数，决定客户的授权，属于业务数据；数据库地址虽然也会改，仍属于部署配置。本文不为没有需求的参数搭建配置中心、Redis 或微服务。

## 2 业务数据完整清单

以下覆盖当前方案内的业务信息，不等于每行都要建一张表。账号、客户成员和动态政策是目标设计；当前没有这些完整数据库模型或管理入口。拟议字段表达需要保存的信息，具体建表、登录协议和账号创建方式在实施前评审，不在这里冒充已确认细节。

### 2.1 身份、机器人和业务政策

| 编号 | 业务数据及完整信息范围 | 当前实现 | 目标来源／写入负责人 |
| --- | --- | --- | --- |
| B01 | 客户：`customer_id`、可识别的客户名称、启用状态；用于隔离数据与权限 | 业务记录携带 `customer_id`，没有完整客户管理模型 | 数据库；客户管理用例。名称、状态的具体字段名待建模 |
| B02 | 用户账号：稳定用户／`actor_id`、登录标识、密码校验信息、启用状态；账号与成员身份不能混为一项 | `users[].actor_id` 和预置 token，网页输入 Bearer 凭据；没有账号密码登录 | 数据库；账号用例。密码安全散列，不存明文密码；登录标识类型、注册方式待确认 |
| B03 | 客户成员和角色：用户与客户关联、成员有效性、`roles`、资料维护／批准／取消等操作的授权关系 | `users[].customer_id/roles` 启动后在内存 | 数据库；成员与授权管理用例。不默认有跨客户管理员或一人跨客户权限 |
| B04 | 用户认证状态：已发凭据的校验引用、失效／撤销依据；若选择有服务端状态的会话，还包括会话与到期记录 | 静态 token 与内存名单，无在线凭据撤销管理 | 数据库保存必要校验和撤销信息；账号用例。Cookie、JWT 或其他方案未定，不先规定会话表 |
| B05 | 机器人登记：`customer_id`、`binding_id`、命令 `wire_robot_id`、可选 `voice_wire_robot_id`、凭据校验引用、启用状态、服务端允许动作、版本 | JSON `robots[]` 建认证内存名单；部分字段首次写入 `ra_robot_bindings`，语音映射仍在配置；无完整在线登记入口 | 数据库；受鉴权登记用例。保留 `dlv/0` 原认证头和 ID，不要求改机器人端 |
| B06 | 机器人隔离：`isolation_reason`、`isolation_evidence_refs`；解除隔离所需的事实与授权依据 | `ra_robot_bindings` 持久保存原因和证据；无已确认的自动解除策略 | 数据库；冲突／未知反馈用例及单独获准的维护用例。普通登记、重连不得解除隔离 |
| B07 | 客户授权政策：客户、政策版本、生效引用、动作的 `allow/require_confirmation/deny`、审批有效期 `approval_ttl_seconds`、操作主体的有效资格 | `policy.version/actions/approval_ttl_seconds` 启动读取，政策版本是全局属性 | 数据库；政策维护用例，按客户读取当前版本。缺规则为 `unavailable`，历史批准不是当前政策 |
| B08 | 安全相关管理变更记录：谁在何时改变哪个客户的成员、凭据、绑定或政策，变更前后版本和结果 | 没有完整管理审计模型 | 数据库；对应管理用例同事务保存必要审计。记录字段及保留时长在实施前确定，秘密值不进入审计 |

自带账号登录已由用户确认。账号由管理员创建还是公开注册、登录会话形式、跨客户管理方式没有确认；这些不改变 B01—B04 的数据库边界，但相关接口实现前必须确认，不能把建议当需求。

### 2.2 资料、输入、计划和执行事实

| 编号 | 业务数据及完整信息范围 | 当前实现／权威来源 | 写入负责人和更新方式 |
| --- | --- | --- | --- |
| B09 | 能力资料：实际技能 ID 的含义、适用机器人、来源、版本、维护者和时间；服务端动作许可见 B05/B07，协议参数结构见第 5 节 | `ra_resource_versions`，`kind=capability`；不是图数据库，也不是模型编造的能力 | Resources 保存不可变版本；维护者权限当前从静态身份取得，改造后从当前成员记录取得 |
| B10 | 环境资料：真实路点 ID／名称与业务含义、来源、版本、维护者和时间；实际机器人上报路点另见 B12 | `ra_resource_versions`，`kind=environment` | 同 B09；不存在默认演示桌子、水杯或路点 |
| B11 | 资料生效指针：客户、机器人、资料类型、当前 revision | `ra_resource_heads` | 资料版本和生效指针同事务更新；运行计划保持原版本引用 |
| B12 | 机器人观察：连接代次、connected、hello 的 skills/capabilities/runtime_id/busy_cmd_id，以及 readiness/telemetry；每字段 value、received_at、connection_generation | `ra_robot_bindings.observations` 和 `connection_generation` | 当前认证连接经 TaskService 反馈用例更新；新连接需新证据，无关字段不能刷新旧字段时间 |
| B13 | 输入处理记录：音频请求摘要、幂等键、转写状态、转写文字、提供方引用、生成任务关联；文字请求的原任务目标与幂等记录 | `ra_inputs` 保存音频处理事实；文字在 `ra_tasks/ra_request_keys`；原始音频不默认保存 | InputService 和 TaskService；同键同内容不重复建任务，保存失败不虚构成功 |
| B14 | 用户任务：客户、发起主体、目标、状态、原因、恢复位置、版本、队列序号／时间、活动计划／执行单元、完成步骤、取消请求、结果争议、创建与更新时间 | `ra_tasks` | TaskService 按合法状态和事务写入；排队不是已发送，超时不是失败事实 |
| B15 | 澄清问题和回答：当前问题与 clarification_id、answers；拟议 required_slots、回答关联与状态 | 当前嵌入 `ra_tasks`；没有独立澄清表，候选 required_slots 未完整持久化 | TaskService.answer；与原任务和可信主体关联，不把不同人的回答并入同一任务 |
| B16 | 计划及步骤：plan_id、task_id、revision、step_id、action、args、depends_on、服务端 source_refs、完整快照 | `ra_plans.steps/snapshot`；只保存采纳的计划，候选不是获准指令 | TaskService 采纳事务保存不可变版本；重新规划建新版本，不改在途 wire 内容 |
| B17 | 计划上下文快照：客户／绑定、代次、技能、路点和 skill 引用、就绪证据、能力／环境语义、绑定版本、snapshot_id、valid_until | 嵌入 `ra_plans.snapshot`，不是可随意替换的配置 | Resources 构造，TaskService 随计划持久化；下发仍查当前资格与条件 |
| B18 | 检查与授权判断：类型、结果、规则／政策版本、任务／计划／步骤、证据引用、phase、actor_id、base_result 与检查时间 | `ra_decisions` 的 validation/authorization；与当前政策分开 | Preflight 产生，TaskService 同事务保存；记录过去判断不代表永久获准 |
| B19 | 人工决定和批准证据：decision_id、客户、任务、计划／revision、scope、approver_id、policy_version、issued_at、expires_at、revoked_at、结果 | `ra_decisions` 的批准证据；拒绝当前通过任务／事件表达，拒绝者完整审计尚需补齐；不是可编辑“已批准”开关 | TaskService.decide；下发重查有效性和审批者资格；未来拒绝决定也保留操作主体和原因，不覆盖历史 |
| B20 | 执行单元：unit_id、任务／计划／绑定、状态、下一步骤、版本 | `ra_units`；首期一个任务一个单元，没有多机分工方案 | TaskService 与任务／指令同事务更新，不能独立维护第二套完成结论 |
| B21 | 指令事实：command_id、归属关系、step_id、attempt、wire_skill/args、下发代次／owner、下发状态、结果、证据／冲突、版本、准备／发送／结束时间 | `ra_commands` | TaskService 下发／反馈／恢复用例；已可能下发的内容不可改写或盲目重发 |
| B22 | 机器人执行反馈和受限诊断：event_id、绑定、代次、cmd_id、类型、received_at、state/detail/error_code 或诊断原因 | `ra_robot_events`；就绪／遥测最新观察在 B12，不保证每个历史采样都有事件行 | TaskService 校验归属后保存；错误来源不推进，秘密和任意大音频不落诊断 |
| B23 | 客户可见任务事件：task_id、seq、task_version、event_type、public_payload、时间 | `ra_task_events` | 状态事务追加，查询按客户和权限过滤；事件不是另一个执行引擎 |
| B24 | 写请求幂等：客户、主体、操作、key、request_digest、result_ref、时间 | `ra_request_keys`；音频受理另有 B13 | TaskService 与业务写入原子保存；相同文本不等于同一请求 |
| B25 | 活动任务执行占用：deployment_id、客户／任务／单元／绑定、owner_run_id、state | `ra_execution_guard`；系统侧维护的业务占用事实 | TaskService/Store 执行事务；未知在途结果不释放，重启不清空 |

B12 的 readiness 全部现有字段：`executor`、`skill_server_ready`、`busy`、`outbox`、`map`、`waypoints`、`waypoint_names`、`preload`、`nav_server_ready`、`brain_server_ready`、`brain_engine`、`brain_ckpt`、`brain_prompt_profile`、`brain_preset`、`brain_skills`。telemetry 为 `battery/location/observed_at`。这些是机器人报告的数据，即使字段里有“profile/preset”，也不是云端部署配置。

用户视图、计划摘要、结果说明、自然语言回复及播报文本从 B14—B23 派生，不再建一份可独立判定成功的权威记录。原始模型输出／完整聊天历史、音频、TTS 成品音频、视觉图片、长期记忆、能力图数据库和多机协同数据不在当前必需持久化清单；未来确有需求时再设计，不把示例推成需求。外传与保留政策需启用前确认。

### 2.3 当前所有数据库表及字段核对索引

下表是固定源码基线的核对索引，不是另一个迁移定义；实际 DDL 以 `db/migrations/0001`—`0007` 和 PostgresStore.migrate 为准。主表与附加迁移合并列出最终字段，JSON 内部信息见上表和下表备注。未来改 schema 时同步更新索引并重跑覆盖检查。

| 表 | 全部字段 | 分类／JSON 内容 |
| --- | --- | --- |
| `ra_tasks` | `customer_id, task_id, actor_kind, actor_id, goal_text, state, reason_code, resume_phase, active_plan_id, unit_id, clarification, completed, result_disputed, version, queue_seq, queued_at, created_at, updated_at, clarification_id, answers, cancel_requested` | B14/B15；completed 和 answers 为数组 |
| `ra_robot_bindings` | `customer_id, binding_id, wire_robot_id, credential_ref, allowed_capabilities, enabled, connection_generation, isolation_reason, isolation_evidence_refs, observations, version` | B05/B06/B12；observations 每字段含 value/received_at/connection_generation；当前 credential_ref 是环境变量名，不是已实现的数据库凭据校验 |
| `ra_resource_versions` | `customer_id, kind, resource_id, revision, payload, source_refs, created_by, created_at` | B09/B10；payload 当前为条目标识到语义字符串的映射 |
| `ra_resource_heads` | `customer_id, binding_id, kind, revision` | B11 |
| `ra_inputs` | `customer_id, actor_id, operation, key, input_id, request_digest, state, normalized_text, provider_ref, result_task_id, created_at` | B13；不是原始录音表 |
| `ra_plans` | `customer_id, plan_id, task_id, revision, steps, snapshot, created_at` | B16/B17；steps 每项 step_id/action/args/depends_on/source_refs；snapshot 的全部字段为 customer_id/robot_binding_id/generation/skills/waypoints/brain_skills/readiness/snapshot_id/allowed_actions/readiness_evidence/capability_labels/environment_labels/binding_version/valid_until |
| `ra_decisions` | `customer_id, decision_id, task_id, plan_id, step_id, kind, result, rule_or_policy_version, evidence_refs, detail, created_at, plan_revision, scope, approver_id, policy_version, issued_at, expires_at, revoked_at` | B18/B19；detail 当前为 phase/actor_id/base_result 或批准信息；不同种类不强行共享同一含义 |
| `ra_units` | `customer_id, unit_id, task_id, plan_id, binding_id, state, next_step_id, version` | B20 |
| `ra_commands` | `customer_id, command_id, task_id, unit_id, plan_id, binding_id, step_id, attempt, wire_skill, wire_args, dispatch_generation, owner_run_id, dispatch_state, outcome, evidence_conflict, evidence_ids, version, created_at, sent_at, finished_at, dispatch_started_at` | B21；wire_args 按动作契约；当前没有独立 receipt_observed_at 列，接收依据来自关联事件 |
| `ra_robot_events` | `customer_id, event_id, binding_id, connection_generation, cmd_id, event_type, received_at, payload` | B22；payload 为经过归属／字段约束的状态、详情或诊断 |
| `ra_task_events` | `customer_id, task_id, seq, task_version, event_type, public_payload, created_at` | B23；public_payload 为事件类别对应的状态／原因／决定等摘要 |
| `ra_request_keys` | `customer_id, actor_id, operation, key, request_digest, result_ref, created_at` | B24 |
| `ra_execution_guard` | `deployment_id, customer_id, task_id, unit_id, binding_id, owner_run_id, state` | B25；不是部署参数的可编辑副本 |
| `ra_runtime_owner` | `deployment_id, owner_host_id, owner_run_id, state, started_at` | 系统持久记录；Runtime 独占维护，不向客户 API 暴露 |
| `ra_schema_migrations` | `version, checksum` | 系统持久记录；迁移组件维护，不能改已安装脚本绕过校验 |

## 3 部署配置完整清单

当前正式装配读取 JSON 与环境变量一次，以下参数变化按受控重启生效；不是说参数永远不会变化。重启前先停新下发、保留在途事实，再按总规格恢复。技术限额收紧影响新受理或新下发，不删除既有记录、不把在途动作当未执行。

### 3.1 已有正式入口读取的参数

| 编号 | 参数／引用 | 作用与缺失处理 |
| --- | --- | --- |
| C01 | `RO_AGENT_CONFIG_FILE` | 定位 JSON；当前未设置时页面可打开，业务依赖不自动变成可用 |
| C02 | `database.dsn_env` 及它引用的环境秘密，例如 `RO_AGENT_DATABASE_URL` | 启动前连接 PostgreSQL；缺连接引用／值不能完整装配。DSN 可能含密码，不进入业务响应或日志 |
| C03 | `database.schema` | 应用表所在 schema，当前缺省 public；不作为运行期给普通客户切换数据范围的字段 |
| C04 | `deployment.id` | 部署执行权和执行槽的稳定身份；不能换 ID 绕过已有占用 |
| C05 | `deployment.execution_enabled` | 此部署是否参与物理下发；当前仅 true 启用。不是客户动作授权，也不证明停机 |
| C06 | `deployment.host_id` | 获准 Linux 主机名；启用执行必须核对实际主机 |
| C07 | `deployment.lock_path` | 所有潜在进程引用的同一绝对、持久锁文件；不能另建路径规避互斥 |
| C08 | `deployment.advisory_key` | 稳定数据库运行锁键；不能随进程变化 |
| C09 | `allow_external_calls` | 部署级外部模型／ASR调用总门禁；true 不代替费用、客户数据外传和凭据使用授权 |
| C10 | `DEEPLEAP_LLM_PROVIDER` | 当前 gpt/deepseek；不支持或未选定时没有可用规划提供方 |
| C11 | `GPT_BASE_URL`, `GPT_MODEL`, `GPT_API_KEY`, `GPT_TIMEOUT_S` | GPT 兼容提供方连接；前三者缺失规划不可用，当前 timeout 缺省 30 秒 |
| C12 | `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_API_KEY`, `DEEPSEEK_TIMEOUT_S` | DeepSeek 同上；当前 timeout 缺省 30 秒 |
| C13 | `VOICE_ASR_PROVIDER` | 当前正式装配仅接 doubao，其他／缺省转写不可用；不切 fake |
| C14 | `VOICE_ASR_DOUBAO_API_KEY`, `VOICE_ASR_DOUBAO_RESOURCE_ID`, `VOICE_ASR_DOUBAO_STREAM_URL` | ASR 连接秘密、资源及端点；缺失实际调用报 unavailable |
| C15 | `limits.asr_timeout` | 启用豆包 ASR 时必须显式正数；不是类构造器中的 15 秒默认 |
| C16 | `limits.freshness_seconds` | 必要就绪／路点／技能字段证据的新鲜度上限 |
| C17 | `limits.max_resource_entries` | 每次资料维护的技术条目上限 |
| C18 | `limits.max_audio_bytes` | 输入音频字节上限 |
| C19 | `limits.max_frame_bytes` | 机器人帧技术大小上限 |
| C20 | `limits.max_text_chars` | 新任务文本长度上限；其他文字写入口是否同样有界须实施时核验 |
| C21 | `limits.max_plan_steps` | 完整计划步骤数量技术上限 |
| C22 | `limits.provider_response_bytes` | 装配注入的供应商响应字节上限；LLM/ASR 各自的额外类默认值见第 3.4 节 |
| C23 | `limits.scan_interval` | 执行循环扫描间隔，不是用户延迟承诺 |
| C24 | `limits.command_timeout` | 对已可能下发的指令等待上限；到期记结果未知，不自动重发 |

C16—C24 当前启动要求为正数。后续实现需明确类型、单位、最大可接受范围和布尔值拒绝规则，不能只因 Python 的 bool 是 int 就接受 true 作为秒数。本轮不修改校验代码。正式部署必须记录所选值；现有 30 秒默认是代码事实，不是用户确认的响应目标，是否保留默认在实施前评审。

### 3.2 当前放在配置、目标应迁入业务数据库的完整字段

| 当前配置路径 | 全部字段／引用 | 目标分类 |
| --- | --- | --- |
| `users[]` | `customer_id, actor_id, roles, token_env` 以及 token_env 引用的真实用户凭据 | B02—B04；不保留普通用户全量名单作为永久部署配置 |
| `robots[]` | `customer_id, binding_id, wire_robot_id, voice_wire_robot_id, token_env, allowed_actions` 以及 token_env 引用的机器人凭据 | B05；allowed_actions 与数据库 allowed_capabilities 是同一许可含义的现有命名映射，不是两份独立政策 |
| `policy` | `version, actions, approval_ttl_seconds`；actions 是客户到动作判定的映射 | B07；客户级政策和当前生效版本不能永久由一个启动内存对象代表 |

用户／机器人认证秘密不是供应商调用秘密：前者是业务认证数据，数据库保存安全校验信息或受控秘密引用；后者供平台调用外部服务，留在部署秘密管理。首次初始化可以经批准从受保护输入导入，导入完成后数据库为准；启动不反复覆盖业务名单。不要把当前 credential_ref 的环境变量名误写成安全散列已实现。

### 3.3 已有类参数、待确定政策和未装配能力

| 编号 | 信息范围 | 分类和当前状态 |
| --- | --- | --- |
| C25 | DoubaoTTS 的 `endpoint, api_key, resource_id, speaker, sample_rate, timeout_seconds, max_audio_bytes` | 若按平台统一 TTS 部署则是部署配置／秘密；类已存在但正式装配未接，尚无确定 ENV/JSON 字段。speaker 当前是提供方音色，不是用户的说话人身份。客户个性化音色不是已确认需求 |
| C26 | VoiceService 的 `allow_woken_audio, duplicate_policy, max_audio_bytes` | 前两项承载业务政策，不能因构造参数就当部署配置；输入大小复用 C18。正式装配未接，机器人重复输入政策仍待确认，不能自动选择 per_turn |
| C27 | 音频／文本外传允许范围、提供方使用授权、原始录音／日志／资料／任务／备份保留与删除政策 | 客户业务政策与平台硬边界需区分；当前无完整持久政策／清理机制。实际值、作用范围及删除安全性在相关能力启用前确认；不预设保存录音或删除未结束证据 |
| C28 | 账号登录所需的部署级签名／加密秘密及初次管理员初始化输入 | 仅在登录方案需要时增加，当前没有确定字段；登录方案和管理员创建方式未定，不直接新增 JWT_SECRET 等“必需配置” |
| C29 | 首版允许参与执行的容量、未来客户额度／并发／多机选机参数 | 当前是一台机器人、部署内一个活动任务的程序约束；与机器人登记数据源分开。客户额度和多机策略未定，不先造数据库参数或放开执行 |
| C30 | 队列顺序、自动过期、启动通知行为 | 当前代码按 queue_seq 顺序、无自动过期；仓库 README/验收记录记为后续确认，原 V5 未同步。本次按现有记录同步状态，不新增不同队列政策；启动通知方式仍需核实。队列事实见 B14，算法见第 5 节 |

C25—C30 是完整性核对中发现的参数或政策边界，不代表本次全部批准实施。没有确定命名的项只写信息范围，不伪造一套可启动的环境变量。

### 3.4 开发、部署工具及代码内参数

| 信息 | 归属与当前事实 |
| --- | --- |
| Uvicorn `--factory/--host/--port/--workers` | 进程启动参数；文档命令为 host=127.0.0.1、port=8000、workers=1，不是客户业务数据。正式网络／TLS／代理方式未定，不能将 localhost 示例当部署架构 |
| `RO_AGENT_TEST_DSN` | 仅隔离测试配置，必须是本机且数据库名以 _test 结尾；不进入生产配置或业务库 |
| pyproject.toml 的依赖版本、包资源、pytest/ruff 选项 | 工程配置，随代码发布；不是用户注册或运行期权限数据 |
| ASR wrapper 的 `provider/client/api_key/resource_id/stream_url/timeout_seconds/max_response_bytes`，其中 timeout=15、max_response_bytes=10485760；LLM 类 `config/client/max_response_bytes`，默认上限 65536 | 这些是对象注入与代码内默认参数，不是全部已有部署字段；wrapper 当前仅 provider 和音频大小限制实际作用。正式 LLM 上限已被 C22 覆盖，实际 ASR 网络超时来自 C15；wrapper 额外音频限制仍在，实施时核对一致性，不把默认当批准值 |
| 模型 `temperature=0/response_format=json_object`、固定提示词；ASR `model_name=bigmodel/enable_itn/enable_punc/enable_ddc`、16k 单声道 16bit WAV 约束及音频分块；供应商 `uid=ro-agent` | 当前适配器代码参数和格式规则，尚无部署热配置入口；调整需契约验证。提示词不是数据库权限依据 |
| 客户端轮询 700ms、任务事件查询每批上限 100、迁移事务锁键 719002601 | 当前内部实现参数／工程规则，不是已有 JSON 参数；未来要参数化需说明实际收益，不默认为必需管理项 |

## 4 在线业务变更怎样生效

### 4.1 读写责任与生效契约

| 变更 | 目标读取／生效点 | 对已有任务的影响 |
| --- | --- | --- |
| 新增／停用用户或客户成员、调整角色／撤销凭据 | 管理事务提交后，下一次认证、业务写操作和指令下发的资格检查读取数据库当前事实；旧 TrustedContext.roles 不是持续授权 | 保留历史 actor_id；权限不足暂停新下发，继续核对已发送动作的反馈，不能宣称物理停止 |
| 登记／禁用机器人、改凭据或允许动作 | 下一次接入／业务资格检查使用当前绑定；建立连接与最终写入门再次核验 enabled、凭据／资格状态及版本 | 旧连接不能因仍存在而继续获准；保留历史 binding_id 和在途证据。身份映射变更影响在途归属时拒绝在线改写，另行维护评审 |
| 更新客户政策／批准有效期 | 一个事务保存新不可变版本、更新客户生效引用及审计；受理、审批、出队和实际写入前按该客户重查 | 旧政策版本的批准失效；待执行任务回相应等待／阻塞状态，已完成步骤不重做。新有效期不回写旧证据 |
| 审批者被停用／失去审批资格，或批准被撤销／到期 | 读取批准时保留 approver_id，执行前查当前成员资格与证据时效 | 旧批准不继续放行。当前存储读取遗漏 approver_id、Preflight 未完整复查资格，这是待修代码缺口，不因迁表自动解决 |
| 更新能力／环境资料 | 新版本及 head 同事务生效；后续快照读当前版本，下发核对与计划一致性 | 不静默替换已采纳参数；条件冲突时阻塞并核实 |
| 业务数据库不可用 | 认证、资格、政策或必要证据读取失败返回 unavailable／阻塞新下发 | 不退回旧 JSON 名单或缓存继续执行；可能已发送的动作仍保留未知与占用 |

“下一次检查生效”不意味着可以撤销已经开始的网络写入。指令进入 DISPATCHING 和最终写入门都复查必要资格／版本；与管理变更的事务顺序按既有锁约定协调。已经开始或无法确认的发送视为可能已下发，继续保存和核对证据，不重发、不释放未知占用。不得为了用户停用而丢弃机器人完成反馈。

首期直接查 PostgreSQL，不引入权限缓存。未来加缓存必须给出失效规则和撤权最大延迟并另行评审，否则不能声称“在线立即生效”。IdentityPort 的当前主体读取改异步时，所有调用点显式 await；PolicyPort 返回当前客户政策版本，TaskService 不能继续用全局 self.policy.version 当所有客户的版本。

### 4.2 迁移与初始化

1. 在隔离库新增账号／客户成员／政策／必要管理审计结构和绑定补充字段；保持已有 customer_id、actor_id、binding_id 和全部任务／计划／指令关联。完成条件：旧记录可查，跨客户外键与访问测试仍成立。
2. 在实施前确认的维护窗口内，从明确批准的静态名单生成导入预览；检测 ID 冲突、重复身份、语音映射、缺凭据和非法权限。完成条件：经负责人核对导入对象，不从生产环境随意抓取秘密。
3. 显式执行一次初始化，保存校验／秘密引用而非明文密码，并记录来源及版本；重跑不覆盖后续在线改动。完成条件：导入可核对、幂等，失败不留半份权限关系。
4. 将身份、政策和登记读取切到数据库，正式启动不再维护 users/robots/policy 全量业务清单；保留部署配置及明确初始化入口。完成条件：新增、停用与授权变更无需改文件重启，数据库失败不沿用静态名单。
5. 在持锁、核对实际在途结果后恢复执行；回滚软件不能回滚物理动作或重新放行已撤销凭据。完成条件：未结束任务和未知占用保留，回滚路径不使用过期授权；未满足时保持停发。

这里迁移的是本服务的静态登记与数据来源，不授权导入旧平台未结束任务、删除历史记录或操作真实机器人。

## 5 不应塞进配置文件或普通业务表的内容

| 信息 | 放在哪里，为什么 |
| --- | --- |
| dlv/0 消息、动作参数类型、别名、状态合法性和结果未知不重发规则 | 代码／契约测试；客户允许哪些已有动作在 B05/B07，客户不能热改协议格式或打开安全旁路 |
| 当前允许的 move_to/vla_skill 业务支持范围与 ActionCatalog | 代码契约。当前 deployment.py 手写必需字段，应在后续实施中核对与 protocol/validation 的一致性；不是将协议全量词表存进可编辑业务政策 |
| WebSocket/send 函数、RobotSession、asyncpg pool 和持锁 connection、flock 文件句柄、asyncio Lock/Event/Task、VoiceService.sessions/notifications、PCM 缓冲及临时音频／供应商响应 | 内存；不可序列化恢复连接。与持久连接代次、观察、指令、执行槽不同 |
| TrustedContext、TaskInput、CandidatePlan/CandidateStep、ValidationResult、CheckResult、ChangeSet、SendObservation、Transcript | 调用中的业务对象；其中需要追踪／恢复的事实归 B13—B23，不要求每个对象建表。SendObservation.sent 不能当动作已完成 |
| TaskView、结果摘要、语音帧和页面状态 | 从持久事实派生，当前轮次／页面在内存；事实来源不因展示或 TTS 改变 |
| ra_runtime_owner、ra_schema_migrations；代码／schema 版本与校验和 | 系统持久记录／发布元数据；不是由客户在线编辑的业务配置 |

## 6 完整性核对、依据与未确认项

### 核对范围和完成判据

完整指：固定源码基线的正式配置读取、全部现有表／列、业务模型和 JSON 载荷、语音增量构造参数、启动／测试／工程参数，以及本次已确认的账号登录目标均有分类和状态说明。不指尚未定义的视觉、长期记忆或多机算法也已经穷举字段。

核对来源：app.py、config.py、deployment.py；api 全部入口；application 全部用例；adapters 的 identity/policy/postgres/dlv0/llm/asr/tts/voice_output；runtime.py、domain/models.py、protocol/dlv0_schema.py；全部 7 个 SQL 迁移；client/app.js、pyproject.toml 和测试 DSN 读取；总规格、部署和验收记录。没有读取实际凭据、生产配置或生产数据库。

| 核对项 | 通过条件 |
| --- | --- |
| 配置完整性 | 每个正式 JSON／ENV 读取字段都有第 3 节对应；业务名单和政策不误记为永久部署配置 |
| 数据完整性 | 全部 15 张现有表及最终列在第 2.3 节；JSON 载荷、快照、语音输入及拟议账号信息有对应类别 |
| 生效完整性 | 身份、政策、绑定、批准、资料的更新与在途影响均有第 4 节规则，缺配置／缺库行为明确 |
| 文档一致性 | 总规格／部署／实施／验收入口一致指向本文；静态配置仍需用于当前代码，目标示例不能冒充当前可运行配置 |

本次核对结论与数量见[文档核对记录](data-configuration-review.md)。该记录只证明文档覆盖与一致性，不证明新能力已经运行。

### 原则依据

[Twelve-Factor Config](https://12factor.net/config) 区分随部署变化的连接／秘密和应用内部规则；它本身不替本项目规定全部业务表。[FastAPI 官方模板用户路由](https://github.com/fastapi/full-stack-fastapi-template/blob/master/backend/app/api/routes/users.py)通过数据库创建和管理普通用户，[首次初始化](https://github.com/fastapi/full-stack-fastapi-template/blob/master/backend/app/core/db.py)与普通用户管理分开。[Django 用户与权限管理](https://docs.djangoproject.com/en/5.2/topics/auth/default/)采用持久用户／权限，并明确[权限缓存](https://docs.djangoproject.com/en/5.2/topics/auth/default/#permission-caching)可能让旧对象继续使用旧资格。因此“放数据库”不自动等于撤权立即生效。[Django Constance](https://django-constance.readthedocs.io/en/latest/backends.html)说明需要在线维护的参数可以持久化；本项目不因此引入该组件。设备在线登记的参考为 [The Things Stack 设备管理接口](https://www.thethingsindustries.com/docs/api/reference/grpc/end_device/)，不是直接复制其部署架构。

本方案由已确认的账号登录、多客户隔离和在线业务管理需要推导；参考项目说明可行做法，不是“大家都这么写”就成立。

### 实施前仍要确认的范围

账号创建方式／登录标识和会话方案、管理操作主体与跨客户权限、机器人凭据轮换及在途身份映射维护、客户政策修改资格／批准／生效流程、外传与数据保留／删除政策、机器人重复语音受理政策、具体部署限额值及现有默认参数的处理，仍需相应功能实施或启用前确认。本轮不新增问卷、不默认任何具体选择。未来多机与视觉在进入对应阶段时另行设计。

下一步：先审阅专项实施计划中的 D1 字段与契约设计，再另行批准代码实施范围；不能仅根据本次文档更新开启生产执行。
