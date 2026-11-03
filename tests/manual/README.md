# 本机模拟机器人拿水联调

从项目根目录运行。平台地址为 `http://127.0.0.1:18763/`，机器人使用已登记的 `mock-fridge-01` 和环境变量 `RO_AGENT_ROBOT_TOKEN`。同一编号只启动一个客户端。若本机启动入口已启动该机器人，直接在网页提交任务即可。

## 单独启动客户端

在 PowerShell 中读取随分支保存的场景资料，并连接已运行的平台。

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
