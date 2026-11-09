# PCM 语音切句

2026-11-09 新增可选 `voice_pcm.wav_save_dir`：在 WAV 转换完成后保存录音，未配置不保存，文件写入错误只记日志并继续处理。配置、实际端到端结果和中断事项见 [WAV 保存说明](wav-recording.md)。以下按日期保留历史切句开发记录。

当前范围：平台将连续 PCM 切成 WAV，已接入机器人 WebSocket。默认只切句；显式打开测试任务模式后，复用已有官方转写、输入落表和任务规划。已用单台本机模拟器完成一次 PCM → 官方转写 → 中转 gpt-6.1-sol 规划验收，未实现唤醒检测。

## 第 1 步：切句服务

```python
from robot_agent.application.pcm_segment_service import PcmSegmentService

service = PcmSegmentService()  # 从环境读取切句参数
session = service.new_session()  # 每条已认证连接独立创建
try:
    for pcm_bytes in incoming_pcm_chunks:
        for segment in service.feed_pcm(session, pcm_bytes):
            wav = segment.wav_bytes
finally:
    service.close_session(session)  # 断连时丢弃未完成的声音
```

真实入口应在连接建立时创建 session，在该连接整个生命周期内多次 feed，断线才 close。
`feed_pcm` 输入是解码后的原始 **16 kHz、单声道、16 位小端 PCM bytes**，不是 Base64 或 WAV。
PCM 本身没有格式头；调用者必须确认设备格式，不在这里猜测或重采样。
同一 session 串行使用；两个机器人或两次连接不能共用 session。同步模型计算与构造须放到工作线程，不能阻塞未来的异步收音入口。

返回空列表代表还没有合格语音段。每段有 `segment_id`、`wav_bytes`、`duration_ms`、`end_reason`。
`end_reason` 为 `silence` 或 `max_duration`；达到上限只是截断，不代表用户说完，也不授权创建任务。
强切后仍接收同一句后续声音，短尾音也保留；新一句过短的声音会丢弃。
本模块没有自动重试、上传去重或数据库写入；段编号和结束原因供后续接入与记录使用。

## 配置

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `VOICE_PCM_SILENCE_MS` | 512 | 连续无语音多久结束 |
| `VOICE_PCM_MIN_SPEECH_MS` | 288 | 合格人声累计时长，不计前导和静音 |
| `VOICE_PCM_MAX_SEGMENT_MS` | 15000 | 单个 WAV 的最大时长（含前导和尾静音）；按 32 ms 检测窗向下取整，实际为 14976 ms |
| `VOICE_PCM_PREROLL_MS` | 384 | 起音之前保留多久 |
| `VOICE_PCM_THRESHOLD_START` | 0.5 | 开始有人声的判断门槛 |
| `VOICE_PCM_THRESHOLD_CONTINUE` | 0.3 | 已开始说话后的继续判断门槛 |
| `VOICE_PCM_MAX_CHUNK_BYTES` | 320000 | 一次 feed 最多接收的字节数 |

Silero 每次检测 32 毫秒。最小时长、等待时长和前导向上取整；最大时长向下取整，确保输出不超限。
非法配置在创建服务前报错。单个超限输入块直接拒绝；允许合法小块跨字节或检测窗边界。
模型出错会关闭该 session 并抛错，不把失败当静音。关闭后必须新建 session，旧半句不续接。

## 依赖与验证

在独立开发环境安装 `pip install -e ".[dev,voice-pcm]"`；CPU 机器可先从 PyTorch 官方 CPU 索引安装 `torch==2.10.0`。
Silero 使用安装包自带的模型权重，feed 不访问网络。普通平台启动不导入这些可选依赖。

确定性行为测试：`python -m pytest tests/unit/test_pcm_segment_service.py tests/unit/test_pcm_config.py`。
真实模型测试：`python -m pytest tests/contract/test_pcm_silero.py`，没有可选依赖时明确跳过。
后者使用已有 `tests/manual/fixtures/front-desk.wav`（“请去前台”），拆成 641 字节小块，检查仅输出一段、格式正确、包含原语音全部非零样本且样本值未改变。

这些测试不调用 ASR/LLM、不连接机器人、不启动平台，不构成第 2、3 步联调验收。

## 2026-11-05 第 1 步验收记录

- 实施前，新增契约测试因切句模块尚不存在而失败；实施后 51 项确定性/配置测试通过。
- 独立测试环境中，2 项真实 Silero 测试通过：把“请去前台”拆成 PCM 后只得到 1 段 1.984 秒 WAV，结束原因为静音，原语音全部非零样本保留且数值不变；纯静音不输出 WAV。
- 强切后的短尾音、弱尾音保留；纯静音尾巴不生成伪语音段，新一句短噪声仍丢弃。
- 现有环境运行 unit + contract：125 通过、5 跳过。其中 3 项需要独立 PostgreSQL 测试库，本步未运行；2 项是上述真实 Silero 测试，已在独立环境补验通过。
- Ruff 和 git diff --check 通过。没有改动运行平台、启动装配、机器人入口、ASR/LLM 配置或数据库。

## 第 2 步：WebSocket 与模拟器

平台配置增加 `"voice_pcm": {"enabled": true}` 才启用；默认关闭。切句参数继续从上表环境变量读取。

认证后，模拟器在一条持续连接上发送多次 JSON 消息：

```json
{"type":"audio","pcm_b64":"这一包原始 PCM 的 Base64"}
```

已认证的语音连接也接受真机每约 5 秒发送的 `{"type":"stats", ...}` 麦克风状态消息：直接跳过，不保存、不回复，不触发切句、转写或任务提交。状态消息不会计入 PCM 包数，也不会清空正在接收的半句音频。身份校验、消息大小及 JSON 对象检查仍然生效；其他未知消息仍按原规则拒绝，命令连接的处理不变。

每条语音连接独立保存切句状态。平台先返回 `state: thinking`，再返回以下诊断结果，最后返回 `state: idle`。该结果是本步测试接口，现有真机不需要理解其中的 WAV 字段。

```json
{
  "type": "pcm_segment",
  "segment_id": "本段编号",
  "duration_ms": 1984,
  "end_reason": "silence",
  "received_chunks": 138,
  "received_pcm_bytes": 88320,
  "wav_b64": "平台实际生成的 WAV"
}
```

PCM 路径不调用 `VoiceService`、ASR 或任务受理。既有 `segment` 整段 WAV 入口和命令连接保留。断线时丢弃未结束语音，下次连接不拼接上次半句；计算在线程中执行，清理等待尚未完成的计算结束。

模拟器新增 `send-pcm`：读取已有 WAV、去掉文件头，默认前后补各 1 秒静音，按真实音频速度每 20 毫秒发送 640 字节；收到 `thinking` 后停止继续发送，不自动重传。命令连接由另一个 `serve` 常驻进程保持。

```powershell
# 先配置 RO_AGENT_ROBOT_TOKEN 和 RO_AGENT_TEST_WS_URL，绑定必须有对应的语音 ID。
# RO_AGENT_TEST_WS_URL 指向明确选择的测试平台 /api/voice/robot/ws 地址。
python -m tests.manual.mock_robot_ws --url $env:RO_AGENT_TEST_WS_URL serve --waypoint 前台
python -m tests.manual.mock_robot_ws --url $env:RO_AGENT_TEST_WS_URL send-pcm tests/manual/fixtures/front-desk.wav --output-dir C:/Temp/pcm-acceptance-new
```

输出目录保存 `segment-001.wav` 和 `pcm-upload-report.json`，已有结果不会被覆盖。`sent_chunks` 是客户端实际成功发送的包数；`received_chunks` 是平台生成该段时已处理的累计包数，反馈延迟可能让两者略有差别。

## 2026-11-05 第 2 步实际验收

- 分支 `codex/wav-simulator`；新增路由与模拟器测试 19 项通过，包含前一步的候选发布测试合计 72 项通过；Ruff、补丁格式检查通过。
- 本机平台已切换到增量发布，保留原运行版本的其他功能；数据库备份 15 张表内容核对一致。模拟器 `mock-fridge-01` 命令连接已建立并保持运行。
- **仅发送一次**“请去前台”：一条语音 WS 连接，实际发送 **138 包 / 88,320 字节**，平台产出时处理 **138 包 / 88,320 字节**；收到 `thinking` 停发剩余尾静音。
- 平台实际生成 **1 段 WAV，1,984 毫秒，63,532 字节**；16 kHz、单声道、16 位 PCM；结束原因 **`silence`**，即连续无语音达到 512 毫秒，并非达到最长时长。
- 输入夹具 1.93 秒。输出与补静音后的原 PCM 连续样本完全一致，全部原始非零样本保留；未转写推断内容。
- 原业务表计数前后相同：任务 4、输入 0、计划 1、指令 1。PCM 路径没有调用转写、规划或下发任务。
- WAV 与报告：`D:/ro-agent-data/acceptance/pcm-20261105-155135/upload-001/`；完整验收记录：其上级目录的 `acceptance-result.json`。
- 未验证：真实机器人麦克风、真实环境噪声、唤醒、ASR、规划。PCM 结果目前保存为本机验收文件，尚未新增音频结果业务表。

## 第 3 步：复用已有转写和规划（2026-11-05）

新增配置 `"voice_pcm": {"enabled": true, "submit_tasks": true}`。默认 `submit_tasks=false`，收到音频或客户端发送 `woke=true` 都不能自行打开任务模式。这是当前单台模拟器的明确测试开关，不能代替真机唤醒判断。

`VoiceService.deliver_pcm()` 用已认证的机器人身份，把静音结束的完整 WAV 交给已有 `InputService.audio()`；后者调用官方 ASR，将转写存入 `ra_inputs`，再进入网页文字共用的 `TaskService.submit()`。服务端 `segment_id` 同时作为输入去重键和关联键。同段重复提交只转写、建任务一次；跨重连重新切出的新段不在本步去重范围内。

任务模式下强切段返回 `PCM_TOO_LONG` 并结束本次语音连接，后续残句一起丢弃；下一连接重新开始。默认只切句模式仍保留原有强切结果。

平台仍返回实际 `pcm_segment`，另外返回 `pcm_task`（`segment_id/request_key/task_id/state`）。现运行平台异步规划，收到 `pcm_task` 只说明任务已受理，验收须继续按 `task_id` 查询计划。该路径不调用语音合成，不产生 `TTS_UNAVAILABLE` 干扰。

模型选择继续读配置：`VOICE_ASR_PROVIDER=openai`、`VOICE_ASR_OPENAI_MODEL=gpt-transcribe`；`DEEPLEAP_LLM_PROVIDER=relay`、`RELAY_MODEL=gpt-6.1-sol`。官方密钥继续从已有 Windows 凭据 `ro-agent/openai-asr` 读取，未写入仓库。当前正式发布已包含 wav 分支的官方 ASR 实现与装配，同时保留原运行版本的其他功能。

模拟器用 `send-pcm ... --expect-task --timeout 300 --output-dir <新目录>`；默认不加 `--expect-task` 时仍兼容只切句验收。

### 实际结果

- 一次上传，发送/平台处理均 **138 包、88,320 字节**；输出一段 **1,984 毫秒 WAV**，结束原因 `silence`。
- 官方实际转写：**请去前台。**；提供方引用 `openai:gpt-transcribe:req_75ffd008d07944edbd1972d7ca8535d6`。
- 任务编号：`d405e021-a7fc-43eb-8b66-765c51e5bfe6`；输入编号：`dc133470-8acf-4ebc-a46e-f55f910e2cd8`；音频段编号：`44f55f6c-9967-4b3d-a1c5-d5bbf2d4184c`。
- gpt-6.1-sol 规划并被平台保存：`step_1`，`move_to`，`{"location":"前台"}`，无前置步骤。
- 计划编号：`446fa08c-9bbc-447a-ab2d-9dc2a0726782`；任务状态 **WAITING_APPROVAL**，原因 `AUTHORIZATION_REQUIRE_CONFIRMATION`。本轮只验收转写和规划，没有下发移动指令。
- 表计数前后：输入 0→1、任务 4→5、计划 1→2、指令 1→1；没有重复任务。
- 接线/模拟器/官方 ASR 契约/真实 Silero 目标测试 49 通过、3 项数据库测试最初跳过；随后启用独立测试库，相关数据库及官方 ASR 契约测试 12 通过，补验全部跳过项。测试库使用独立随机 schema。Ruff 与 diff 检查通过。
- 平台重启成功，模拟器自动重连；切换前备份的 15 张表逐表内容核对一致。
- 本次完整证据：`D:/ro-agent-data/acceptance/pcm-task-20261105-162643/acceptance-result.json`；音频和发送报告在同目录 `upload-001/`。
- 尚未验证：真机麦克风和现场噪声、唤醒、真实机器人执行。本步没有新增音频原文件业务存储表；WAV 为本机验收文件，转写和任务计划已落原有业务表。
