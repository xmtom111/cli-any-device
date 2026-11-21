# Jubilee 适配器约束

## Duet / RepRapFirmware

真机默认使用以太网 HTTP。独立模式通过 `rr_connect` 建立会话，读取 `rr_model`，使用 `rr_gcode` 发送指令并按回复序号读取 `rr_reply`。SBC 模式使用 `machine/connect`、`machine/model` 和 `machine/code`。密码由本机配置提供。RRF 2.x 的兼容性须单独验证。

science-jubilee 提供回原点、运动、取放工具、安全 Z、载架和孔位变换。工具切换由机器配置中的 T 指令及 tpre、tpost、tfree 和工具锁宏完成。真机的宏、已安装工具、软限位、锁传感器及物理急停须现场确认。缺失遥测保持未知。无现场配置及监督时只做只读探测、预检或离线演练。

独立模式 `/rr_gcode` 的 HTTP 200 只表示请求获响应。入队、固件执行和达到动作后置条件须分别判定。动作之后执行 M400，确认屏障执行结束并读取对象模型 idle，再由内核检查相关轴、坐标、工具及锁状态。SBC 的同步代码回复也要配合对象模型检查。

M112 走独立 HTTP 请求，绕过动作互斥。M112 不切断动力电。首次运动前由操作者验证物理急停或掉电继电器。急停后的检查、复位与重新回原点由操作者和共享运行时负责。

## MuJoCo 孪生

只读复用 AutoDNA A 的 CAD-backed XYZ 运动学和几何代理。正 Z 在该模型中使床远离喷嘴。轨迹和接触代理可用于仿真检查，模型不积分接触力。源模型没有动态换刀机构和真实锁传感器。取刀、停刀、U 回原点和工具锁只可作为明确标记的逻辑仿真。孔位与安全 Z 使用仿真载架，不能推断真机布局或证明真机无碰撞。

## GRBL 验收

**GRBL 仿真不是真 Jubilee。** 识别结果必须表明 GRBL 仿真，能力仅限 XYZ 运动子集。取刀、停刀、工具锁、解锁和 T 宏必须拒绝，且不得发送相应协议帧或伪造工具状态。用实时复位停止并清空模拟队列，回原点状态随复位失效。等待 Idle，不能用 G-code 确认代替运动完成。

AutoDNA 的队列深度、超时、进给、行程与管架示教点属于实验设计，未经缺失的 ESP32 草图或真实 Jubilee 的 config.g 校验。

## 本地运行与验收

适配器使用公共 `adapter_spi` 的 `AdapterConfig`、`AdapterPair`、`ActionRequest` 和标准遥测类型。每个适配器子目录可独立安装，注册 `cli_any_device.adapters` 下的同名入口。也可将各目录的 `create_adapter` 工厂直接注入守护进程。CLI 的 dry-run、确认门和动作后置条件在共享运行时执行。

- MuJoCo 和 GRBL 默认读取 `/workspace/autodna_sim_a`。可用工厂选项 `autodna_root` 或环境变量 `CLI_ANY_DEVICE_AUTODNA_ROOT` 指定源目录。同一进程使用同一份 AutoDNA checkout。
- MuJoCo 默认装载模型中固定的 `autodna_deck` 和 1 槽的 `autodna_rack`，含 96 个坐标。加载命令可重新登记相同布局。孔位 Z 偏移以模型的管口平面为零，台面以上的管高单独报告。动作沿路径以最多 1 mm 的间隔采样代理接触，工具 0 的附着与 U 轴锁为逻辑状态。
- GRBL 在进程内复用原模拟控制器，回原点走 `$H`，急停走实时 `0x18`。支持绝对、相对和安全 Z 的 XYZ 运动。局部回原点和原始 G-code 不开放。默认不连接 TCP 端点。状态中的无可换工具来自模拟器结构，工具锁和传感器读数保持未知。
- Duet 工厂在具备实际 endpoint 和显式密码时连接。密码取本机工厂配置或 `CLI_ANY_DEVICE_DUET_PASSWORD`。未配置时报告离线。配置后提供只读检测、预检和状态，以及显式请求的独立 M112。M400 监督接口用于确认运动屏障。独立模式通过 HTTP M118 唯一标记核实屏障回复，再读取 idle。当前 ActionDriver 拒绝物理动作和软件复位。安装 Duet 插件的 `rehearsal` 可选依赖后，可用工厂选项 `vendor_rehearsal=true` 创建离线适配器，经 SPI 排练绝对 XYZ，结果只含厂商生成的帧。
- M112 在本地模拟或 HTTP 测试夹具上验证，现场传输时限、宏、锁传感器和动力切断由真机验收覆盖。

在本仓库根目录运行适配器合规测试。可用 AutoDNA 的既有 Python 环境提供 MuJoCo 和 pytest。若未安装可选的 science-jubilee，只有厂商排练用例会跳过。

```sh
PYTHONDONTWRITEBYTECODE=1 /workspace/autodna_sim_a/.venv/bin/python \
  -m pytest -p no:cacheprovider tests/adapter_compliance
```

官方接口参考见 [RRF HTTP](https://github.com/Duet3D/RepRapFirmware/wiki/HTTP-requests)、[DSF REST API](https://duet3d.github.io/DuetSoftwareFramework/articles/rest-api.html) 和 [science-jubilee Machine API](https://science-jubilee.readthedocs.io/en/latest/autoapi/science_jubilee/Machine/index.html)。
