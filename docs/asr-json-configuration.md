# v1 ASR 的 JSON 配置

ASR 供应商、模型、地址和超时统一写在应用 JSON 的 `voice_asr` 中。应用顶层的
`credentials_file` 指向本机密钥 JSON。启动器读取一次、校验一次，向应用传入
`AsrConfig`；直接通过 `robot_agent.app:create_app` 启动时也使用同一个读取函数。

这次统一范围是 ASR。数据库、用户/机器人令牌及规划模型的现有加载方式保持原职责，
本机规划模型与 ASR 共用顶层 `credentials_file` 所指的 JSON，不再使用
`llm.credentials_file`。ASR 不读取 `VOICE_ASR_*` 环境变量或 Windows 凭据管理器。

## 在本机源码中配置豆包

编辑项目的 `config/dev.local.json`，保留其他节点，增加或修改：

```json
{
  "allow_external_calls": true,
  "credentials_file": "D:/private/ro-agent-secrets.json",
  "llm": {"provider": "relay"},
  "voice_asr": {
    "provider": "doubao",
    "mode": "stream",
    "model": "bigmodel",
    "resource_id": "volc.bigasr.sauc.duration",
    "stream_url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
    "timeout_seconds": 90
  }
}
```

上面是需要合并的片段，不是完整平台配置。`credentials_file` 改为自己的绝对路径；
`resource_id` 填账号实际开通的流式识别资源，示例不表示账号已有权限。

在对应的密钥 JSON 中增加字段，保留原有规划模型等条目：

```json
{
  "VOICE_ASR_DOUBAO_API_KEY": "填写自己的豆包语音 API Key"
}
```

键名中的 `VOICE_ASR_` 只是 JSON 字段名，不要求设置环境变量。
保持原来的 `bash start-dev.sh` 启动方式。修改配置后，需要重新启动该源码实例才生效。
启动器会打印所选 ASR 供应商，不打印密钥。

## 切换 OpenAI

将整个 `voice_asr` 节点改为：

```json
{
  "voice_asr": {
    "provider": "openai",
    "mode": "file",
    "model": "填写账号可用的转写模型 ID",
    "timeout_seconds": 90
  }
}
```

密钥 JSON 提供 `VOICE_ASR_OPENAI_API_KEY`。只校验当前选中供应商的密钥，
不会因为未选中的另一个供应商没配置而阻止启动。豆包与 OpenAI 的参数不同，
切换时应同时调整 `mode`、`model` 等该供应商所需字段。

## 校验和迁移

- 缺少密钥、模型、豆包资源/地址，或超时不是有限正数时，启动报配置错误。
- `voice_asr.provider` 拼错时立即报错，不偷偷切换提供方。
- `voice_asr` 缺省或 `provider=disabled` 表示不启用 ASR；关闭
  `allow_external_calls` 时也不加载 ASR 密钥。收到音频仍按原有不可用错误处理。
- 超时唯一来源是 `voice_asr.timeout_seconds`；删除旧的 `limits.asr_timeout`。
- 旧的 `VOICE_ASR_PROVIDER`、`VOICE_ASR_OPENAI_MODEL`、豆包资源和 URL 环境变量不再生效。
- 使用旧 Windows OpenAI 凭据的部署，需要由用户把密钥填入选定的 JSON；
  本次没有读取、导出或迁移 Windows 凭据。
- `tests/manual/local_platform.py` 通过 `RO_AGENT_CONFIG_FILE` 定位同一份应用 JSON，
  不再用 ASR 环境变量。录音验证入口读取 `config/dev.local.json`，可用
  `--credentials-file` 覆盖密钥文件路径；旧 `--provider-env-file` / `--asr-credential` 已移除。

修改源码不会自动修改、发布或重启 `D:/ro-agent-data/releases/` 下的运行副本。
配置层测试只验证配置解析和装配；真实 ASR、页面、数据库、模型和机器人完整链路需另外验收。

## 本次检查记录（2026-11-12）

- 配置解析用例 28 项全部通过；覆盖供应商选择、旧环境字段不生效、缺密钥、空白模型、无效超时和地址。
- 使用项目启动配置指定的 Python 运行三个相关测试文件：39 项通过，2 项失败，3 项跳过。两个失败均来自已有 `StoreFixture` 缺少 `conversation_messages`，在 ASR 装配之前失败；原提交版本已有该不匹配，本次未扩改。三个数据库集成用例因未提供测试数据库条件而跳过。
- 9 个相关 Python 文件的语法及 Ruff 静态检查通过。以上是开发检查，不作为真实平台或机器人验收结论。
- 本机配置已选择豆包，实际凭据 JSON 校验报缺少 `VOICE_ASR_DOUBAO_API_KEY`；需要在该文件补齐密钥才能启动使用豆包。未修改凭据、未调用真实 ASR，未启动或更新正式平台。
