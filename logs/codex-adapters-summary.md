# Codex 适配器交付摘要

三个目标使用公共 `adapter_spi` 类型，按 `cli_any_device.adapters` 注册工厂。

- `mujoco_twin` 只读复用 AutoDNA A 的 MJCF 和运动学。通过检测、预检、dry-run 回原点、仿真回原点、逻辑取刀、孔位运动、停刀与急停。孔位结果经 XYZ 关节读数验证，代理接触数为零。U、工具附着和锁均为逻辑状态。载架为固定的 96 孔仿真模型。
- `grbl_sim` 在进程内复用 AutoDNA GRBL 控制器，实现 XYZ 运动和实时复位。模拟器没有可换工具，锁和传感器状态保持未知。取刀、停刀、锁、解锁与 T 宏均返回 `E_UNSUPPORTED_CAPABILITY`，且没有下发对应协议帧。
- `duet_rrf` 提供独立模式和 SBC HTTP 的只读检测、遥测、M400 完成屏障与显式独立 M112。独立模式须收到与屏障关联的 HTTP 消息并观察到 idle。HTTP 200、无关回复与提前 idle 均无法证明完成。固定版本的 science-jubilee 提供离线绝对 XYZ 帧排练。物理动作派发和软件复位禁用。本次没有连接真机，现场宏、传感器、急停时延与物理停机仍待真机验收。

整仓 **48 项通过**，含 **21 项适配器合规用例**。验证包含 CAD 模型、GRBL 运动、固定版 science-jubilee 离线排练、两种 Duet 协议夹具和 SBC 环回 HTTP。第二进程经 Unix IPC 停止 GRBL 对角运动，验证急停通道耗时低于 100 ms、停止位置稳定及回原点标志失效。三个插件 wheel 均完成构建和入口工厂加载验证。

约束和复现方式见 [docs/adapter-constraints.md](../docs/adapter-constraints.md)。
