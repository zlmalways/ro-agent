# 第六段：地图和移动模拟验收

当前客户端先声明身份，默认第 10 秒首次上报状态，此后每 10 秒一次；`--report-interval` 可以显式覆盖。动作开始／成功／失败立即反馈，不等下次状态周期。地图消息包含地图名、地点清单、数量、`preload=loaded` 和导航就绪；反馈的 `progress=1` 只表示结束，成功与否看 `state`。

当平台显式启用 `resources.use_reported_waypoints` 时，地点直接来自当前有效上报，不必另外登记；技能仍须原有登记和检查。未收到首条状态、地图缺失或过期时，不能把机器人当成可执行。状态有效期没有因为模拟器周期而放宽。

第六段用独立库、随机端口和本地模型测试接口验证“移动到冰箱旁（模拟）”；见 `tests/acceptance/test_simulated_movement.py`、`tests/manual/test_simulator_alignment.py` 和 `docs/acceptance.md`。WAV 上传继续原用法，本段只回归其协议，不调用真实 ASR。没有控制真机或启动 18763，VLA 不作为本段真机能力验收。

以下为已有拿水联调示例，端口地址仅是显式参数示例，不表示服务当前正在运行。

# 本机模拟机器人拿水联调

从项目根目录运行。平台地址为 `http://127.0.0.1:18763/`，机器人使用已登记的 `mock-fridge-01` 和环境变量 `RO_AGENT_ROBOT_TOKEN`。同一编号只启动一个客户端。若本机启动入口已启动该机器人，直接在网页提交任务即可。

## 单独启动客户端

运行 `serve`、`send-wav` 或 `send-pcm` 时都要在子命令前填写 `--url`，指定平台的 WebSocket 地址。下面的 PowerShell 示例使用本机 `18763`，连接其他平台时请换成对应地址。

```powershell
$scene = Get-Content -Encoding UTF8 -Raw tests/manual/fixtures/fridge-water.json | ConvertFrom-Json
$robotArgs = @('--url', 'ws://127.0.0.1:18763/api/voice/robot/ws', 'serve',
               '--robot-id', $scene.robot_id, '--map', $scene.map, '--duration', $scene.duration_seconds)
foreach ($name in $scene.environment.PSObject.Properties.Name) { $robotArgs += @('--waypoint', $name) }
foreach ($skill in $scene.capabilities.PSObject.Properties.Name) { $robotArgs += @('--skill-id', $skill) }
python -m tests.manual.mock_robot_ws @robotArgs
```

机器人会主动上报航点和技能编号。规划还需要平台保存相同编号的环境和技能说明，本机 18763 已登记。在新测试库中，通过平台的资料维护入口将场景文件的 `environment` 和 `capabilities` 条目分别合入现有资料。平台的 `GET /api/robot-agent/resources/snapshot` 可以核对当前机器人、在线上报和对应说明。

## 验收

在网页输入“去冰箱拿一瓶水”并批准计划。核对先下发 `move_to` 到“冰箱旁”，收到成功回执后再下发 `vla_skill`，参数 `skill_id` 为 `take-bottled-water`。第二次提交同一任务，在第一步执行时点取消，核对机器人收到对应指令编号的取消、任务结束为 `CANCELLED`，且拿水指令未下发。

用 `python -m pytest -q tests/manual/test_mock_robot_ws.py` 在本机临时 WebSocket 端口运行客户端协议回归。


## 连续十次数据库断线回归

使用独立测试 PostgreSQL，将 `RO_AGENT_TEST_DSN` 指向名称以 `_test` 结尾的测试数据库后，从项目根目录运行：

```powershell
python -m pytest tests/manual/repeat_runtime_owner.py
```

该入口重复现有的数据库锁丢失检查十次：断开旧进程的数据库连接后，旧进程不能恢复下发或取消；关闭旧进程后新进程能接手。它只重复已有测试逻辑，默认全套测试不额外收集这十轮。不要把 DSN 配成业务数据库。


## WAV 与 PCM 语音上传

命令连接和语音连接使用同一枚测试机器人凭据，但使用平台登记的两个连接编号。语音编号默认 `mock-fridge-01-voice`，可用 `--voice-id` 覆盖；平台地址在子命令前通过 `--url` 明确指定，不使用默认地址。

整段 WAV 上传要求 16 kHz、16 位、单声道 PCM WAV。`fixtures/front-desk.wav` 的内容为“请去前台”。每次只发送一条 `segment` 消息，标记 `woke=true`；连接中断后不自动重发。

```powershell
python -m tests.manual.mock_robot_ws --url ws://127.0.0.1:18763/api/voice/robot/ws send-wav tests/manual/fixtures/front-desk.wav
```

PCM 上传读取同一 WAV 的原始音频，默认前后各补 1 秒静音，每 20 毫秒发送 640 字节；保持一条语音连接，收到平台 `thinking` 后停止发送，不重连、不重播。目标平台须显式启用 `voice_pcm.enabled`；若还要转写和任务受理，另需启用 `voice_pcm.submit_tasks`，客户端参数不能开启服务器功能。

```powershell
python -m tests.manual.mock_robot_ws --url ws://127.0.0.1:18763/api/voice/robot/ws send-pcm tests/manual/fixtures/front-desk.wav --output-dir C:/Temp/pcm-acceptance-new
# 只在目标平台已配置任务受理时增加 --expect-task：
python -m tests.manual.mock_robot_ws --url ws://127.0.0.1:18763/api/voice/robot/ws send-pcm tests/manual/fixtures/front-desk.wav --output-dir C:/Temp/pcm-task-acceptance-new --expect-task
```

结果目录保存平台实际返回的 `segment-001.wav` 和 `pcm-upload-report.json`，不覆盖已有结果。报告记录发送包数、音频字节数、WAV 时长、结束原因；`--expect-task` 额外要求收到关联的 `pcm_task` 回执。回执表示输入已受理，不表示异步规划已经完成或机器人已经执行，后续须按任务编号查计划和结果。`TTS_UNAVAILABLE` 单独记为警告；转写、规划错误或缺少所需回执仍判失败。完整切句与任务接线说明见 `docs/pcm-segmentation.md`。

## 独立 WAV 联调平台与真实调用边界

历史手动接线 `python -m tests.manual.local_platform` 使用专用数据库 `ro_agent_sim_test`，要求 `RO_AGENT_TEST_DSN` 指向 `127.0.0.1`，并分别设置 `RO_AGENT_ROBOT_TOKEN`、`RO_AGENT_USER_TOKEN`。该入口固定监听本机 `18763`，仅适用于该端口未被占用的独立联调；上述客户端也可显式连接另外启动的测试平台。该手动入口主要用于整段 WAV，不会因为客户端选择 `send-pcm` 而自动打开 PCM 功能。

手动平台通过环境配置选择转写供应商：默认豆包，使用 `VOICE_ASR_DOUBAO_API_KEY`，可覆盖 `VOICE_ASR_DOUBAO_STREAM_URL` 和 `VOICE_ASR_DOUBAO_RESOURCE_ID`；选择官方 OpenAI 时使用 `VOICE_ASR_PROVIDER=openai` 及对应的 `VOICE_ASR_OPENAI_*` 配置。规划模型继续使用平台已有环境配置，密钥只放本机环境，不写进模拟器代码。

离线协议回归使用 `python -m pytest -q tests/manual/test_mock_robot_ws.py tests/manual/test_simulator_alignment.py`，只连接临时本机端口，不调用真实模型。`test_live_robot_voice.py` 则须先明确允许真实调用，用 `RO_AGENT_TEST_WS_URL` 指定独立测试平台的 WebSocket 地址，再设置 `RO_AGENT_RUN_LIVE_VOICE=1`，它会上传一次 WAV、查询转写与任务并核对导航指令和模拟成功回执；重新运行会产生新输入。本轮合并未运行这项真实联调，不代表真机能力已验收。
