# PCM 转换后的 WAV 保存

2026-11-09 当前交付：保存逻辑已实现，正常保存的真实端到端测试通过；无配置场景在任务规划后遇到 `RESOURCE_EXPIRED`，已按用户要求停止。用户选择暂不排查。不能将本次交付称为全部测试通过。

## 配置与处理规则

在平台配置 JSON 的 `voice_pcm` 中添加可选字段 `wav_save_dir`。本机文件为 `config/dev.local.json`，当前内容为：

```json
"voice_pcm": {
  "enabled": false,
  "evaluate_wake": false,
  "submit_tasks": false,
  "wav_save_dir": "D:/ro-agent-data/testdata/recordings"
}
```

这三个原有开关保持关闭；本轮只用临时测试配置启用语音、唤醒、任务和执行。日常运行 `bash start-dev.sh` 仍按上述本机配置启动，不会自动加载模型密钥或启用完整语音任务链路。

- 没有 `wav_save_dir`，或值为 JSON `null`：不保存录音。空白字符串或其他字段类型会在启动装配时被拒绝。
- 配置目录后：PCM 切句模块在完成 WAV 编码后保存同一份字节，再交回原来的转写和任务链路。未完成的半句、被切句规则丢弃的声音没有 WAV，因此不保存。
- 按北京时间（UTC+8）分目录，格式为 `YYYY-MM-DD/YYYYMMDD_HHMMSS_微秒_片段编号.wav`。目录按需创建，独占新文件写入，避免覆盖已有录音。
- 文件写入发生系统错误时，记录 `PCM WAV save failed`、片段编号、路径和异常堆栈，然后继续返回 WAV 给转写和任务处理。没有增加重试，也没有把保存失败改成语音任务失败。
- 保存发生在唤醒判断和转写之前，因此配置后保存的是切句模块产出的 WAV，并不限于最终成功创建任务的语音。

实现位置：`src/robot_agent/application/pcm_segment_service.py` 的 `_finish` / `_save_wav`；配置接入位于 `src/robot_agent/deployment.py` 的 `build_pcm_segments`。

## 本次真实验证

使用分支 `codex/test-platform-launcher`，基线提交 `a3bb6f7ed4b8b71427c4ea30562f5377b00bb371` 加本次未提交改动。报告记录了两个改动源码文件的 SHA-256。仅连接测试平台 `127.0.0.1:18764` 和 PostgreSQL `127.0.0.1:55440/ro_agent_sim_test`。

测试音频由 Windows 中文语音合成生成“小千小千，请去前台”，16 kHz、单声道、16 位 PCM。模拟器按 20 毫秒分包发送；平台使用真实 Silero 切句、OpenAI `gpt-transcribe` 转写、现有 relay 规划模型、任务确认接口、调度和 PostgreSQL 存储。提供方秘密只在进程内读取，没有复制到项目。模拟器回执只证明平台处理回执，不证明真机移动。

| 场景 | 实际观察 | 验收状态 |
| --- | --- | --- |
| 配置保存目录 | 生成并保存 1 段 WAV；真实转写“小千小千，请去前台。”；规划为前台移动；经原确认接口批准后下发；收到并落库 `running` / `succeeded` 回执，任务 `SUCCEEDED` | 通过 |
| 删除保存字段 | 收到 1 个 WAV 片段、真实转写和任务受理回执；随后任务变为 `BLOCKED / RESOURCE_EXPIRED` | 中断；未完成不落盘和完整任务闭环的验收 |
| 保存目录不可写后仍继续处理 | 已记录 `PCM WAV save failed`；同一片段真实 ASR 完成，`ra_inputs.state=COMPLETE`，转写为“小千小千，请去前台。”；任务已创建但随后变为 `BLOCKED / RESOURCE_EXPIRED`，没有进入下发 | 保存错误隔离和转写通过；完整任务闭环待资源有效期问题处理后补测 |

第一轮任务：`9c16ec98-ff89-4cf6-b686-69cd6fe67960`。录音为 3.456 秒、110,636 字节，逐字节等于平台返回的 WAV：

```text
D:\ro-agent-data\testdata\recordings\2026-11-09\20261109_211930_591436_26c67312-b7ea-41c4-aee3-3344e129be91.wav
```

SHA-256：`b29c91f520a42e31c1c8c394261c5031977b9088c327dfe2f55e1f1ea90e6420`。

本机证据：

- `test-db/wav-recording-verification.json`：总状态为 `failed`，明确记录第二场景中断；包含第一场景完整证据。
- `test-db/wav-e2e-20261109T131923Z/`：各轮上传报告和平台返回的 WAV。
- `test-db/launcher-wav-save.log`、`test-db/launcher-wav-without-config.log`：实际平台启动与正常退出日志。
- `tests/acceptance/verify_pcm_recording.py`：需要显式授权真实提供方调用的验收入口，首次失败即停止；未使用业务 mock。
- `test-db/wav-write-failure-verification.json` 及其目录：保存失败场景的错误日志、真实输入记录和阻塞原因；本次任务为 `7c04a81b-1130-4e6b-8248-5c2684d8263b`，输入为 `COMPLETE`，任务阻塞原因为 `RESOURCE_EXPIRED`。

AST 语法和 `git diff --check` 已通过。本轮是改动后验证，没有先取得新增行为的失败测试证据；不声称完成 TDD 红绿流程。未运行全项目回归、真机测试、保存失败后的完整机器人下发回执或磁盘满场景。

停止时两轮测试平台均通过 Ctrl+C 正常退出，测试模拟器已断开；临时应用/启动配置和合成输入已删除，验收证据保留。默认本机配置中的原有功能开关未改变，正式平台未部署本次改动。测试库保留本轮实际产生的任务与回执，不再是空历史库。
