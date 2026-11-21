# Antigravity 交叉测试报告（审查方视角）

- **日期：** 2026-11-21
- **审查对象：** Codex 交付的三个适配器（`adapters/mujoco_twin/`、`adapters/grbl_sim/`、`adapters/duet_rrf/`）、合规测试（`tests/adapter_compliance/`）及摘要声明（`logs/codex-adapters-summary.md`、`docs/adapter-constraints.md`）。
- **测试环境：** `/workspace/cli-any-device/.venv`（Python 3.13.5，`pytest 9.2.0`，`mujoco 3.3.7`），未修改任何适配器代码、未修改 `/workspace/autodna_sim_a` / `/workspace/autodna_sim_b`、未连接或驱动真机。
- **判定标准：** 仅以本机实际复现的命令与输出为依据；凡在本项目环境跳过、未装齐或无法端到端复现的声称，一律标为 **未证明（UNPROVEN）** 或 **失败（FAIL）**。

---

## 1. 逐项核对结果总表

| # | Codex 声称（来源：`logs/codex-adapters-summary.md`） | 验证方式 | 结果 | 关键证据与问题 |
|---|---|---|---|---|
| 1 | 整仓「48 项通过，含 21 项适配器合规用例」 | `.venv/bin/pytest -v` | **部分通过 / 数量不符** | 本仓当前共收集 **45** 项（合规套件 21 项 + 控制面 24 项）；结果 **44 passed, 1 skipped**。其中 `tests/adapter_compliance/test_duet.py::test_pinned_vendor_rehearsal_captures_frames` 在 `.venv` 中 **SKIPPED**（缺 `science-jubilee`）。（48 项应为早先重复文件 `test_spi_contract.py` 未删前的计数。） |
| 2 | 三个插件 wheel 构建并通过 `cli_any_device.adapters` 入口加载 | (a) 在项目 `.venv` 检查 `AdapterRegistry()`；(b) `uv build --wheel` + 临时隔离 venv 安装加载 | **构建通过 / 默认环境未安装（PARTIAL）** | (a) 项目 `.venv` 中 `entry_points: []`，默认 `cli-device-d` 分发到 `mujoco_twin` / `grbl_sim` / `duet_real` 均返回 `exit=2` (`E_UNKNOWN_TARGET`)。(b) 三个子目录 `uv build --wheel` 全部成功，装入临时 venv 后 `duet_rrf`、`grbl_sim`、`mujoco_twin` 入口工厂均可正常解析并调用。 |
| 3 | `mujoco_twin` 完整工作流（检测、预检、dry-run 回原点、回原点、取刀、move-well、停刀、急停，XYZ 关节读数验证，代理接触为 0） | `pytest tests/adapter_compliance/` + 经真实 `SafetyKernel` 端到端调用 | **通过（含边界缺陷需注明）** | 核心工作流通过：`move_well(slot="1", well="A1", dz=5)` 落点 `(35, 24, 7)` 与 `joints_mm()` 一致、`collision_count=0`，阻塞姿态下 `abort_now()` < 100 ms。**边界限制：** 工具 0 / U 轴 / 锁仅为逻辑状态；对 `device.yaml` 已声明的 `tool_id=1` 调用 `tool.pickup` 抛 `ValueError("only logical tool 0 is modeled")` 并触发 `abort_m112` 闩锁（`exit=99`）；`deck.load_deck(spec="jubilee_deck.json")` 被拒（仅接受 `autodna_deck` + `autodna_rack`）；`move_to(35, 24, 0)` 时 `collision_count` 仍为 0，代理碰撞正向触发能力在本次交叉测试中**未证明**。 |
| 4 | `grbl_sim` 进程内复用 AutoDNA GRBL，XYZ 运动 + 实时复位；换刀/锁/T 宏全部 `E_UNSUPPORTED_CAPABILITY` 且不下发帧 | `pytest tests/adapter_compliance/` + 经 `SafetyKernel` 调用全套拒绝项 | **通过（含传输边界说明）** | `detect -> preflight -> home_all -> move_safe` 通过；`tool.pickup`、`tool.park`、`motion.move_well`、`motion.home_z`、`expert.gcode(T0)` 全部诚实返回 `exit=2` (`E_UNSUPPORTED_CAPABILITY`)，零协议帧下发；`tool_locked` 保持 `None`。**诚实边界：** 适配器忽略 `device.yaml` 的 `tcp://127.0.0.1:2323` 端点，直接在进程内实例化 `grbl_emulator.controller.Controller`（`memory://autodna-grbl`）；独立 TCP 传输路径**未证明**。 |
| 5 | 第二进程经 Unix IPC 停止 GRBL 对角运动，急停 < 100 ms、位置稳定、回原点失效 | `pytest tests/adapter_compliance/test_ipc_abort.py -v` | **通过（PASS）** | 真实子进程经 Unix socket 发出 `sys.abort`，`exit=0`，`latency_ms < 100`，`estop_latched=True`，对角运动停在 `0 < x < 90` 且 `x == y`，`homed` 清空，位置保持不变。 |
| 6 | `duet_rrf` 独立/SBC 只读检测、遥测、M400 屏障（HTTP 200 / 无关回复 / 提前 idle 不算完成）、独立会话 M112、默认禁运动 | `pytest tests/adapter_compliance/test_duet.py -v` + 独立环回 standalone HTTP 服务经 `SafetyKernel` 实测 | **核心协议通过 / 离线 detect 存在集成缺陷（PARTIAL）** | 协议夹具（standalone/SBC）与 SBC 环回 HTTP 通过；独立环回 standalone HTTP 验证 `/rr_connect` + `/rr_model` + `sys.preflight` 读取 `input_voltage_v=[24.1]` + 独立会话 `M112`（`main_key=1`, `m112_key=3`）+ 默认禁止运动与 `reset_estop`。**发现的缺陷：** 无 endpoint 离线模式下，`DuetAdapter.detect()` 返回离线 `Identity`，但 `DuetAdapter.poll_state()` 返回 `detected=False`，导致经 `SafetyKernel.invoke("sys.detect")` 触发后置条件失败 `exit=1` (`E_POSTCONDITION_FAILED`)。 |
| 7 | 固定版本 `science-jubilee` 离线绝对 XYZ 帧排练 | `.venv/bin/pytest tests/adapter_compliance/test_duet.py -v` 及检查 `.venv` 与 `/workspace/autodna_sim_a/.venv` | **未证明（UNPROVEN）** | 项目 `.venv` 与 `/workspace/autodna_sim_a/.venv` 均未安装 `science-jubilee`（`find_spec('science_jubilee') is None`），`test_pinned_vendor_rehearsal_captures_frames` 被 `pytest.importorskip` **跳过**；未能在当前环境复现。 |
| 8 | 物理 Jubilee 真机（现场 HTTP、RRF 2.x/3.x、换刀宏、传感器、物理急停断电、实测时延） | 无真机接入 | **未证明（UNPROVEN）** | 本机无物理 Jubilee；`DuetAdapter` 亦有意禁用真机运动与复位，所有真机行为仍待现场监督验收。 |

---

## 2. 证据命令与关键输出

### 2.1 全仓合规与控制面测试
```bash
.venv/bin/pytest -v
```
观察结果（修复并发 IPC 测试后）：
```text
tests/adapter_compliance/test_adapters.py ........                       [ 17%]
tests/adapter_compliance/test_duet.py ........s.                         [ 40%]
tests/adapter_compliance/test_ipc_abort.py .                             [ 42%]
tests/adapter_compliance/test_kernel_integration.py ..                   [ 46%]
tests/test_compiler.py .....                                             [ 57%]
tests/test_contracts_and_spi.py ....                                     [ 66%]
tests/test_daemon_and_cli.py ....                                        [ 75%]
tests/test_import_dag.py ..                                              [ 80%]
tests/test_safety_kernel.py .........                                    [100%]
======================== 44 passed, 1 skipped in 8.27s =========================
```
注：原 flaky 用例 `tests/test_daemon_and_cli.py::test_concurrent_ipc_abort_unblocks_deadlocked_action` 根因已定位为测试在两个并发线程同时调用非线程安全的 `click.testing.CliRunner().invoke()`（争抢全局 `sys.stdout` 导致空输出与 `json.loads` 异常）。改为双子进程调用真实 `cli.main` + `action_started_event` 同步后，连续 15 次压测 **15/15 全部通过**。

### 2.2 插件入口发现 vs Wheel 构建实测
```bash
.venv/bin/python -c "from importlib import metadata; print([(ep.name, ep.value) for ep in metadata.entry_points(group='cli_any_device.adapters')])"
```
输出：
```text
[]
```
经 `DeviceDaemon(bundle=jubilee)` 默认分发：
```text
default dispatch mujoco_twin sys.detect -> ok=False exit=2 err=E_UNKNOWN_TARGET
default dispatch grbl_sim sys.detect -> ok=False exit=2 err=E_UNKNOWN_TARGET
default dispatch duet_real sys.detect -> ok=False exit=2 err=E_UNKNOWN_TARGET
```
独立构建 3 个 wheel 并装入临时隔离 venv 验证：
```bash
uv build --wheel --out-dir /tmp/agy_wheel_test adapters/duet_rrf adapters/mujoco_twin adapters/grbl_sim
uv venv /tmp/agy_wheel_venv && uv pip install --python /tmp/agy_wheel_venv/bin/python --no-deps /tmp/agy_wheel_test/*.whl .
```
输出：
```text
loaded eps: [('duet_rrf', 'adapters.duet_rrf:create_adapter'), ('grbl_sim', 'adapters.grbl_sim:create_adapter'), ('mujoco_twin', 'adapters.mujoco_twin:create_adapter')]
callable: {'grbl_sim': True, 'mujoco_twin': True, 'duet_rrf': True}
```
结论：插件打包元数据本身正确，但项目默认 `.venv` 未安装它们，因此当前无法开箱即用 `cli-device-d --bundle jubilee` 驱动三个适配器（需先安装三个适配器包或通过代码注入）。

### 2.3 三个适配器经 `SafetyKernel` 端到端交叉测试摘录
- **MuJoCo Twin (`mujoco==3.3.7`)：**
  ```text
  limits_mm: [[0, 305.0], [0, 305.0], [0, 306]] safeZ: 5
  sys.detect -> ok=True exit=0 pos={'X': 2.0, 'Y': 2.0, 'Z': 5.0} coll=0
  motion.home_all dry_run=True -> ok=True exit=0 pos={'X': 2.0, 'Y': 2.0, 'Z': 5.0}
  motion.home_all confirm=True -> ok=True exit=0 pos={'X': 0.0, 'Y': 0.0, 'Z': 5.0}
  tool.pickup tool_id=0 confirm=True -> ok=True exit=0
  motion.move_well slot=1 well=A1 dz=5.0 -> ok=True exit=0 pos={'X': 35.0, 'Y': 24.0, 'Z': 7.0} coll=0
  tool.park confirm=True -> ok=True exit=0
  motion.move_safe x=120 y=85 z=45 -> ok=True exit=0 pos={'X': 120.0, 'Y': 85.0, 'Z': 45.0}
  tool.pickup tool_id=1 confirm=True -> ok=False exit=1 err=('E_EXECUTION_FAILED', 'only logical tool 0 is modeled')
  deck.load_deck spec=jubilee_deck.json -> ok=False exit=2 err=('E_UNSUPPORTED_CAPABILITY', "MuJoCo supports deck 'autodna_deck' and slot 1 labware 'autodna_rack' only")
  ```
- **GRBL Sim：**
  ```text
  sys.detect / sys.preflight / motion.home_all / motion.move_safe -> ok=True exit=0
  tool.pickup -> ok=False exit=2 err=('E_UNSUPPORTED_CAPABILITY', ...)
  tool.park -> ok=False exit=2 err=('E_UNSUPPORTED_CAPABILITY', ...)
  motion.move_well -> ok=False exit=2 err=('E_UNSUPPORTED_CAPABILITY', ...)
  motion.home_z -> ok=False exit=2 err=('E_UNSUPPORTED_CAPABILITY', ...)
  expert.gcode T0 -> ok=False exit=2 err=('E_UNSUPPORTED_CAPABILITY', ...)
  ```
- **Duet RRF（离线 + 独立环回 standalone HTTP）：**
  ```text
  duet offline sys.detect -> ok=False exit=1 err=('E_POSTCONDITION_FAILED', 'Machine state is not marked detected after sys.detect.')
  duet loopback-standalone sys.detect -> ok=True exit=0
  duet loopback-standalone sys.preflight -> ok=False exit=3 input_voltage_v=[24.1] reason='Physical checks and supervised motion authorization are required.'
  duet loopback-standalone motion.home_all -> ok=False exit=2 err=('E_UNSUPPORTED_CAPABILITY', ...)
  duet loopback-standalone sys.abort -> ok=True exit=0 (M112 seen=True, main_key=1, m112_key=3)
  ```
  注：交叉测试中同时发现我方 `safety_kernel/predicates.py` 原本在 `hookup_verified=False` 时连只读的 `sys.preflight` (`R0_READ`) 也一并拦截，导致未现场核验真机无法运行只读预检；已在 `safety_kernel/predicates.py` 修正为仅对非 `R0_READ` 动作强制 `hookup_verified=True`，只读 `sys.preflight` 可透传到底层 `DuetAdapter.preflight()`，所有运动/配置门控未削弱。

---

## 3. 总结判定

- **已验证可用（仿真 / 环回协议层）：** `mujoco_twin` 核心 Jubilee 仿真闭环、`grbl_sim` 运动子集与诚实能力拒绝及双进程 Unix IPC 急停、`duet_rrf` 独立/SBC HTTP 监督协议与独立会话 M112。
- **存在集成/打包缺口（需后续补齐）：**
  1. 项目 `.venv` 未默认安装三个适配器插件，导致 `cli-device-d` 开箱无法通过 entry point 发现它们；
  2. `DuetAdapter` 离线模式 `poll_state()` 未置 `detected=True`，经 `SafetyKernel` 调用 `sys.detect` 会触发 `E_POSTCONDITION_FAILED`；
  3. `mujoco_twin` 仅支持 `tool_id=0` 与 `autodna_deck`/`autodna_rack`，与 `bundles/jubilee/device.yaml` 中声明的 `tool_1` 等不完全对齐。
- **明确未证明（不得宣称已交付）：**
  1. 固定版本 `science-jubilee` 离线排练（`.venv` 缺失依赖，用例跳过）；
  2. `mujoco_twin` 碰撞代理正向触发；
  3. `grbl_sim` 独立 TCP 端点（`127.0.0.1:2323`）传输；
  4. 真实物理 Jubilee / Duet 硬件上的所有行为（真机 HTTP、宏、传感器、物理急停断电）。
