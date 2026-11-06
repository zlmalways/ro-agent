# 不重试与错误展示：隔离发布包验收

`real_no_retry_acceptance.py` 通过真实 HTTP 和 WebSocket 访问单独启动的发布包。TaskService、PostgreSQL、Luna 规划均由该发布实例真实运行；脚本只模拟有明确 `test-no-retry-` 身份的机器人协议对端，不构造模型输出，不访问数据库，不启动或停止任何服务。

脚本硬性只接受 `http://127.0.0.1:18873` 或 `http://127.0.0.1:18874`。正式 18763、机器人 .39、.9、其他主机、重定向及非测试机器人身份均不允许。

## 实例前置条件

- 源码来自完成本次覆盖的正式 release；根代理保留发布包文件校验及启动命令证据。
- 独立数据库、schema、deployment ID、advisory key、lock path；必须只含本次测试绑定。
- 主测试用户具有 `maintainer` 和 `approver` 角色。测试客户的 `move_to` 和 `vla_skill` 政策为 `require_confirmation`。
- 机器人允许 `move_to` 和 `vla_skill`，绑定和 wire ID 均以 `test-no-retry-` 开头。
- 正常实例配置真实 Luna，不得替换规划器、业务服务、存储。故障实例 18874 使用独立配置中真实不可达的模型地址，以验证连接失败。
- 运行者提供仅本机用户可读的凭据文件。脚本不打印 token，也不把请求头写入证据。

凭据文件字段为 `web_token`、`robot_token`、`robot_id`、`binding_id`、`freshness_seconds`、`command_timeout_seconds`。可选字段 `unprivileged_token` 必须属于同一测试客户且无 approver 角色；`source_release` 用于记录来源；故障实例另设 `model_failure_instance: true`。凭据文件由根代理写入受限的 operations/private 目录，不能纳入源码或公开证据。

## 运行顺序

```powershell
& 'D:/ro-agent-data/runtime/pcm-platform-20261105/Scripts/python.exe' tests/manual/real_no_retry_acceptance.py --base-url http://127.0.0.1:18873 --credentials-path D:/codex_chat/agent-platform/operations/no-retry-20261106/private/credentials.json --cases success,duplicate,cancel,no_robot,state_unknown,not_ready,failed,late_success
```

第二组：`queue,clarification,waypoint_removed,skill_removed,waypoint_expired,skill_expired,missing_waypoint,missing_skill,permission_denied`。脚本通过公开资源接口登记两个测试路点和测试技能。每个计划都由用户文字真实进入 Luna；如果模型没有产生所需步骤或澄清，记为 `NOT_VERIFIED`，不伪造输出。

新增 `concurrent_reads`：真实 Luna 规划和两步任务执行期间，并发读取任务历史、首页任务，同时持续检查 `/health` 必须为 200 且可执行。每次 HTTP 失败直接失败，不重试；记录 history/home/health 次数。数据库 deadlock 计数由根代理在同一隔离数据库另行核对前后增量。用于复核任务列表读取与后台更新之间的实际并发问题。

`timeout` 与 `disconnect_unknown` 会留下可能仍在执行的指令，应单独放在一组最后。`conflict` 和 `untracked` 会隔离测试机器人，须分别使用干净隔离实例，或在每项前由根代理明确重建隔离数据库；脚本不解除隔离、不删记录。

故障实例使用 `--base-url http://127.0.0.1:18874 --credentials-path <故障实例凭据文件> --cases model_failure`。

重启分两阶段：先 `--cases restart_prepare`，记录其生成的 `restart-checkpoint.json`；根代理实际重启隔离实例后，运行 `--cases restart_verify --checkpoint <该文件绝对路径>`。prepare 返回 `NOT_VERIFIED`，只有 verify 在实际重启后通过才算重启验收。

`ui_approval_hold` 使用 `unprivileged_token` 真正提交任务并等待真实 Luna 生成 WAITING_APPROVAL。打印不含秘密的任务编号和本次证据目录内 `ui-approval-release.flag` 路径，协议对端保持在线且不批准、不报告动作。根代理用该无审批权限账号在浏览器验证拒绝与只读刷新，完成后在该路径创建标志文件。脚本确认期间零 command、任务仍待批准后退出；默认最长保持 600 秒，超时记未验证。

## 证据与边界

- 每次运行创建独立时间戳目录；逐 case JSONL 保存所有 HTTP 请求、状态码、响应、WS hello/readiness/telemetry、command、cancel、回执。
- `summary.json` 列出 PASS / FAIL / NOT_VERIFIED、未运行项目、HTTP 次数、命令帧和截图可用的 task IDs；各 task JSON 保存完整任务、事件序号和 robot event IDs。按后端新增的 `planner_request_started` 事件统计真实规划调用开始次数；重复提交用例断言只开始一次规划。浏览器截图由根代理另行完成。
- 首轮 `late_success` 同时验证第一条指令回执超时、结果未知、迟到成功只补证据、两步计划第二步不下发、原 key 重复提交不重规划、停止任务不能批准。
- `cancel` 先等同一指令的 SENT 与 RUNNING 已持久化且连续两次读取版本相同，再发第一次取消；换 key 再点取消只出现一条 cancel 帧。正常取消收到 failed 协议终态后必须为 CANCELLED 且 error 为空。没有因 409 自动重发取消。`duplicate` 验证同 key 并发提交仅一个 task，结束后重复提交不再下发。
- `waypoint_removed` / `skill_removed` 在真实计划形成后移除上报；expired 项目停止刷新资源字段，但继续刷新忙闲及就绪，等待真实时效到期，再批准。
- `missing_*` 可能由模型安全拒绝或澄清；只有真实到达的路径才算覆盖，不能称为非法计划校验已执行。
- `permission_denied` 验证无审批角色的凭据收到 403 且没有指令；不等于执行动作政策 deny 的独立路径。
- `send_timeout` 显式返回 NOT_VERIFIED：协议对端不能稳定制造 TCP 写入超时。回执超时和断线不能冒充发送超时。
- 模型 HTTP 出站调用次数无法仅从公开任务接口准确计数；summary 对此留空并要求另附服务或 provider 证据。`planner_request_started` 是真实规划调用开始次数，不能把调用开始等同为模型服务已经收到请求。
- 这些结果不证明物理机器人移动、拿取、导航或安全停机能力。`test_real_acceptance_guards.py` 只离线验证端口/身份限制和脱敏，不运行假业务服务。

脚本退出码：0 为已选择项目全部通过；1 为至少一项失败；2 为没有失败但存在未验证。默认遇到失败停止，`--continue-on-failure` 可继续收集证据。不要仅凭退出码概括未选择的项目。
