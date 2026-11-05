# 第六段：地图和移动模拟验收

当前客户端先声明身份，默认第 10 秒首次上报状态，此后每 10 秒一次；`--report-interval` 可以显式覆盖。动作开始／成功／失败立即反馈，不等下次状态周期。地图消息包含地图名、地点清单、数量、`preload=loaded` 和导航就绪；反馈的 `progress=1` 只表示结束，成功与否看 `state`。

当平台显式启用 `resources.use_reported_waypoints` 时，地点直接来自当前有效上报，不必另外登记；技能仍须原有登记和检查。未收到首条状态、地图缺失或过期时，不能把机器人当成可执行。状态有效期没有因为模拟器周期而放宽。

第六段用独立库、随机端口和本地模型测试接口验证“移动到冰箱旁（模拟）”；见 `tests/acceptance/test_simulated_movement.py`、`tests/manual/test_simulator_alignment.py` 和 `docs/acceptance.md`。WAV 上传继续原用法，本段只回归其协议，不调用真实 ASR。没有控制真机或启动 18763，VLA 不作为本段真机能力验收。

以下为已有拿水联调示例，端口地址仅是显式参数示例，不表示服务当前正在运行。

# 本机模拟机器人拿水联调

从项目根目录运行。平台地址为 `http://127.0.0.1:18763/`，机器人使用已登记的 `mock-fridge-01` 和环境变量 `RO_AGENT_ROBOT_TOKEN`。同一编号只启动一个客户端。若本机启动入口已启动该机器人，直接在网页提交任务即可。

## 单独启动客户端

运行 `serve` 或 `send-wav` 时都要在子命令前填写 `--url`，指定平台的 WebSocket 地址。下面的 PowerShell 示例使用本机 `18763`，连接其他平台时请换成对应地址。

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
