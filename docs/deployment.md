# 首期部署与启用边界

代码可在 Windows 开发；真正机器人执行仅支持用户确认的 Linux 单机进程。当前没有正式主机、机器人目录、凭据或生产限额，不启用执行。Docker 在本机只用于隔离 PostgreSQL 和 Linux 测试，不修改 Docker Desktop 的内部文件。

## 安装与启动

在 Linux 的独立 Python 3.11+ 虚拟环境安装本仓库：`pip install .`。安装包包含网页与 SQL 迁移，不依赖旧平台代码。创建明确的 JSON 配置，通过 `RO_AGENT_CONFIG_FILE` 指定绝对路径；启动入口为 `uvicorn robot_agent.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1`。缺配置时只开放不可用状态与页面，不构造 fake LLM／Store／机器人。

配置引用环境变量名字，真实密钥只写部署环境，不写 JSON、Git、页面或模型上下文。数据库用户需要本应用 schema 的迁移和运行权限；自动迁移有编号与校验和，不改写已安装迁移。启动时只新增明确登记的绑定，不覆盖既有权限或解除隔离。

```json
{
  "database": {"dsn_env": "RO_AGENT_DATABASE_URL", "schema": "public"},
  "deployment": {"id": "<固定部署ID>", "execution_enabled": false},
  "allow_external_calls": false,
  "users": [{"customer_id": "<客户>", "actor_id": "<用户>", "roles": ["user"], "token_env": "RO_AGENT_USER_TOKEN"}],
  "robots": [{"customer_id": "<客户>", "binding_id": "<内部绑定>", "wire_robot_id": "<实际机器人ID>", "token_env": "RO_AGENT_ROBOT_TOKEN", "allowed_actions": ["move_to", "vla_skill"]}],
  "policy": {"version": "<已批准政策版本>", "approval_ttl_seconds": null, "actions": {}},
  "limits": {"freshness_seconds": null, "max_resource_entries": null, "max_audio_bytes": null, "max_frame_bytes": null, "max_text_chars": null, "max_plan_steps": null, "provider_response_bytes": null, "scan_interval": null, "command_timeout": null}
}
```

上面是待填的配置结构，不是可直接启用的演示配置。部署前填写并评审限额、批准有效期和动作政策，所有限额必须为正值。一个客户的动作可明确配置 allow、require_confirmation 或 deny；缺项 unavailable。maintainer 管理资料、approver 批准计划，这些角色从凭据配置读取，不从请求文本读取。当前首版只允许配置一台机器人；多机器人仅有数据归属与执行单元边界。

## 模型与 ASR

沿用旧代码的提供方选择变量 `DEEPLEAP_LLM_PROVIDER=gpt|deepseek` 及相应 `GPT_BASE_URL/GPT_MODEL/GPT_API_KEY` 或 `DEEPSEEK_BASE_URL/DEEPSEEK_MODEL/DEEPSEEK_API_KEY`，模型与 URL 必须显式指定。旧本地代码不能确认实际部署当前选择哪家，因此不猜测默认模型。候选 JSON 始终再做白名单、目录、权限和依赖检查；没有任何模型直接驱动物理动作的旁路。

旧项目 `docs/code-tour/06-voice.md` 明确生产使用豆包流式 ASR。对应 `VOICE_ASR_PROVIDER=doubao`、`VOICE_ASR_DOUBAO_API_KEY`、`VOICE_ASR_DOUBAO_RESOURCE_ID`、`VOICE_ASR_DOUBAO_STREAM_URL`；当前协议接受 16 kHz、单声道、16 bit PCM WAV。新实现没有 Qwen／fake 回退。外部调用只有在配置 `allow_external_calls=true` 并获得费用、音频外传及凭据使用授权后才启用；本次未做真实调用。

协议参考：[OpenAI 结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)、[DeepSeek JSON 模式](https://api-docs.deepseek.com/guides/json_mode/)、[火山引擎大模型流式 ASR](https://docs.volcengine.com/docs/6561/1354869?lang=zh)。JSON 格式支持不代表业务语义合法；服务端验证不可省略。

## 唯一执行权

启用 execution_enabled=true 时还必须提供实际 `host_id`、共享绝对 `lock_path` 和固定整数 `advisory_key`。当前主机名必须与 host_id 相符；锁目录必须预先创建、持久保存，并让此部署所有进程引用同一个对象。不要删除或替换锁文件，不通过换 ID／路径规避互斥。

先取得 Linux flock，再取得独立 PostgreSQL 会话锁；锁连接不回池、不自动重连。所有权丢失后本进程禁止发送，正常重启重新核对。ra_execution_guard 保留未知在途任务，重启不清空；跨主机接管需要独立核实原主机已停止。本机 Linux 容器已测试第二进程拒绝、数据库连接终止、同锁文件、错误锁路径及其他主机拒绝，不能代替正式主机部署验收。

本次 PostgreSQL 测试容器 `ro-agent-postgres-test`、本机端口 `127.0.0.1:55432`、测试数据库 `ro_agent_test` 使用临时数据盘。每例在随机 schema 中运行，结束只清理这个 schema。不要把此容器当生产数据库。
