# dlv/0 协议来源与实现边界

来源仓库 `git@github.com:zlmalways/deepleap_agent.git`，本地参考目录 `E:\CodexWorkspace\deepleap-replace-20260919`。已通过该目录的 `git remote -v` 核对，取用提交 `73918bee311da613bb95fde80d216c1e964f742f`。

- `src/domains/robots/dlv_schema.py`：选择性提取 dlv/0 状态词表、readiness 与 telemetry 字段。未取用 dlv/1 协商开关、通用旧业务或能力授权默认值。
- `src/domains/robots/ws_driver.py`：参考 command `{type, skill, args, cmd_id}` 和 cancel `{type, cmd_id}` 编码。下发成功只表示传输写入；未照搬断线即失败或重连即取消。
- `src/api/routes/voice_ws.py`：保留 `/api/voice/robot/ws`、query `robot_id`、`x-robot-token`、初始 ready 与命令通道 ready 帧。新实现重做可信客户绑定，hello 不能发放权限。

代码在本仓库独立运行，不导入旧仓库模块。`tests/contract/test_dlv0.py` 使用手工核对的协议字面量验证编码／解析；`tests/integration/test_robot_ws.py` 使用真实本地 Uvicorn WebSocket 与 PostgreSQL，加上隔离协议模拟器验证接入。模拟器不是物理机器人，不能证明 B1 的真机技能、历史结果补报或语音行为。

服务端自行分配内部连接代次和事件 ID，不新增机器人 wire 字段。当前认证连接可以报告以前下发的同一持久 cmd_id；旧连接和错误客户消息不能推进。未知 busy_cmd_id 或没有对应在途指令的 busy=true 记录 `UNTRACKED_EXECUTION` 隔离；重连、busy=false 和普通成功反馈不解除隔离。语音输入／输出、部署所有权双锁与实际身份来源在后续任务实现，尚未启用真实机器人控制。


## 2026-11-12 计划应答音频扩展

经用户确认，command 帧增加顶层 `plan_id`，取自已有 `Command.plan_id`，用于机器人将同一计划的多条动作关联为一次应答播报。`cmd_id`、`skill`、`args`、cancel 和回执格式保持不变，不增加数据库字段。旧机器人解析器忽略额外字段；新增播报逻辑在缺少 `plan_id` 时只跳过播报，仍按原规则处理动作。

本次本机代码已准备；协议/去重开发检查不等于真实机器人验收。安装、重启与页面到真实机器人的完整验证状态见 `outputs/plan-ack-20261112/README.md`。
# 本地唤醒音频反馈（2026-11-16）

按用户确认，机器人本地检测并播放提示音后直接上传 PCM，平台不要求唤醒通知或确认。
移除了未部署草案的 voice_hello / voice_ready / wake / wake_state / wake_end / audio.wake_id。
新增的 audio.capture_id 仅区分前后收音，平台首帧直接处理；pcm_activity 反馈已有 VAD
检测到的人声，供机器人决定何时停止上传，不授予或限制平台接收权限。
这些字段由本项目增加，不能声称来自旧 dlv/0 原始协议。
部署配置、兼容规则和未验证范围见 [机器人本地唤醒](local-robot-wake.md)。
