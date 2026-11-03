# 本机模拟机器人和 WAV 联调

在 ro-agent 根目录安装项目和开发依赖 `python -m pip install -e ".[dev]"`。下列命令从项目根目录运行。模拟机器人主动连平台；命令和语音使用同一枚测试设备凭据、两个不同的连接编号。

## 启动

1. 准备一个本机专用 PostgreSQL 数据库 `ro_agent_sim_test`。设置 `RO_AGENT_TEST_DSN`，主机使用 `127.0.0.1`，数据库账号需要建表权限。分别设置随机的 `RO_AGENT_ROBOT_TOKEN` 和 `RO_AGENT_USER_TOKEN`。
2. 真实转写需要 `VOICE_ASR_DOUBAO_API_KEY`。测试平台默认沿用旧项目的豆包流式接口和资源 ID，支持用 `VOICE_ASR_DOUBAO_STREAM_URL`、`VOICE_ASR_DOUBAO_RESOURCE_ID` 覆盖。规划服务沿用项目配置，例如 `DEEPLEAP_LLM_PROVIDER=deepseek` 搭配 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_API_KEY`。密钥只放在本机环境中。
3. 在三个终端分别执行下面三条命令。前两个保持运行，第三个命令每执行一次提交一轮语音。

```text
python -m tests.manual.local_platform
python -m tests.manual.mock_robot_ws serve --waypoint 前台 --waypoint 充电点 --waypoint 冰箱旁 --skill-id sim-pick --skill-id take-bottled-water --duration 5
python -m tests.manual.mock_robot_ws send-wav tests/manual/fixtures/front-desk.wav
```

平台监听 `http://127.0.0.1:18763`。可在 `/health` 检查服务。测试平台复用正式项目的 FastAPI、数据库迁移、语音服务、豆包识别、任务规划及执行代码。在专用测试库上使用 PostgreSQL 会话锁限定一个模拟调度器，只登记 `mock-fridge-01` 及它的语音编号。这里的接线位于 `tests/manual`，与生产部署独立。语音播报未启用，`TTS_UNAVAILABLE` 不影响已受理的输入。`ASR_UNAVAILABLE` 表示转写没有完成。

命令连接发送 `hello`，随后立即上报模拟地图、航点、技能和忙闲状态，默认每 10 秒刷新。支持已上报的航点、可选的模拟技能、动作成功或失败、取消及进程内的重复指令。可用 `--map`、可重复的 `--waypoint`、`--skill-id` 设置上报内容。改变可规划的地点和技能时，还需通过平台的资料入口登记匹配的环境和能力说明。

语音命令只在已登记的语音连接发送一条 `segment` 消息，标记 `woke=true`，等待平台本轮返回。上传要求 16 kHz、16 位、单声道 PCM WAV。样本 `fixtures/front-desk.wav` 的内容为“请去前台”。上传没有跨重连的请求标识，程序不会在连接中断后自动重发。

## 自动验证

`python -m pytest -q tests/manual/test_mock_robot_ws.py` 使用真实的本机 WebSocket 检查消息格式、WAV 字节、导航和反馈。该测试无需外部凭据。

正式验证真实服务时，先按上面步骤启动本机测试平台和模拟器，然后设置 `RO_AGENT_RUN_LIVE_VOICE=1` 并执行：

```text
python -m pytest -qs tests/manual/test_live_robot_voice.py
```

这项测试会向配置好的识别和规划服务发起实际调用，只上传一次 WAV。从专用数据库按本次录音的摘要查找转写和任务 ID，核对豆包转写含“前台”、下发的 `move_to` 指向“前台”、模拟机器人反馈成功，并核对任务最终状态。第一次失败后再次运行会产生新的语音输入。
