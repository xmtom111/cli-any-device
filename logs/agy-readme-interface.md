# 面向公众的 README 与 CLI 界面整理记录

- **日期：** 2026-11-23
- **基线 HEAD：** `73462e1`
- **保留口号：** `Let AI drive all your devices`
- **修改范围：** 仅改动 `README.md`、`cli/main.py`、`daemon/main.py` 与 `tests/test_daemon_and_cli.py`；未改动内核门控逻辑、适配器实现、`adapters/*`、`tests/adapter_compliance/*`、`.gitignore` 或 `autodna_sim_*`；未将 `agent-prompts/`、调研日志与 PDF 纳入 git。

---

## 1. 主要改动说明

### 1.1 重写 `README.md`（面向公开仓库读者）
- 在 1–2 屏内讲清：
  - **是什么 & 为什么存在**：用口号 `Let AI drive all your devices` 开篇，说明如何把任意有接口 + 手册/上位机的硬件编译为声明式设备包、安全 CLI（`cli-device` / `cli-device-d`）、短 `SKILL.md` 与 `HOOKUP.md`。
  - **诚实声明现状与边界**：明确区分「软件 / 离线 `in_memory` / 本地仿真（`mujoco_twin`、`grbl_sim`、回环 `duet_rrf`）已通过测试」与「真实物理设备尚未完成端到端验收」；明确 `bundles/jubilee/` 是首个复杂验证夹具而非产品本身，`tests/fixtures/`（`grbl_engraver`、`scpi_psu`、`open_radio`）均为合成离线测试夹具（非可购真机，也不鼓励射频发射）。
  - **清晰工作链路图**：`接口 + 手册/上位机 (AST 只读)` → `cli-device bundle draft` → `device.yaml + catalog.yaml` → `cli-device bundle build [--check]` → `SKILL.md + HOOKUP.md + cli_schema.json` → `cli-device`（薄客户端）经 Unix IPC 调 `cli-device-d`（共享 `SafetyKernel`）→ 可插拔 `adapters/*`（仅翻译已有接口，不改原厂固件）。
  - **实测可跑通的 Quickstart**：给出两组本地实跑验证过的命令（`jubilee` + `in_memory` 闭环，以及从 `tests/fixtures/scpi_psu/` 现起草/校验/编译 `bench_psu` 并加载动态 `--help`）。
  - **安全边界与退出码**：R0–R4 与默认拒绝、`--dry-run`（含 `--dry-run sys abort`）、`--confirm`、`await_settled` 完成语义、锁外急停与「软件急停永不替代物理断电急停」、诚实能力声明与退出码表。

### 1.2 整理 `cli-device` 与 `cli-device-d` 命令行界面
- **`cli/main.py`：**
  - 支持 `-h` / `--help`，顶层 `--help` 保留精确口号并在末尾提示 `--bundle` 用法与物理断电急停提醒。
  - 重写 `DynamicDeviceCLI.format_commands()`，将命令清晰拆分为 `Device Commands (<device_id>):`（`sys` 置顶、`expert` 置底）与 `Toolkit Commands:`（`bundle`、`repl`），并缓存同一 `ctx` 内的 schema 解析。
  - 动态子命令帮助自动标注风险与确认标记（如 `[R3, --confirm]`、`[R2]`、`[R0, lock-free]`）以及参数数值范围（如 `[0.0..300.0 mm]`）。
  - 改善无 `--bundle` 或无效 `--bundle` 的报错：不再只报 `No such command 'sys'`，而是给出可执行的 `--bundle <name|dir>` / `cli-device bundle list` 提示。
  - 整理普通文本输出（非 `--json`）：展示 `[OK]/[FAIL]`、耗时、紧凑 `state:` / `plan:` 摘要，并在 `E_CONFIRM_REQUIRED` 时给出 `--confirm` 提示；`--json` 信封格式保持 100% 兼容。
  - `repl` 自动沿用顶层 `--bundle`/`--target`/`--socket`/`--json` 选项。
  - `bundle draft --help` 的示例名改为中性 `bench_psu`（不再用 `open_radio` 作首屏示例）。
- **`daemon/main.py`：**
  - 支持 `-h` / `--help`，保留精确口号与 `SIGTERM/SIGINT` 退出及物理急停提示。
  - 捕获 `BundleValidationError`，对无效 `--bundle` 输出干净的 `Error: ...` 并以退出码 `2` 结束（不再打印 Python traceback）。
  - 启动日志增加当前设备包声明的 `targets=[...]` 列表。
- **`tests/test_daemon_and_cli.py`：**
  - 扩展 `test_help_surfaces_exact_product_slogan`，覆盖口号与物理急停提示、缺 `--bundle` 引导报错、`cli-device-d` 无效 `--bundle` 无 traceback 退出码 `2`、以及 `Device Commands (jubilee):` / `Toolkit Commands:` 分组展示。

---

## 2. 本地实际运行过的命令与结果

### 2.1 README Quickstart #1（`jubilee` + `in_memory`）实跑
```bash
.venv/bin/cli-device-d --bundle jubilee --socket /tmp/cli-device-demo.sock &
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock --json sys detect
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock --json sys preflight
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock --json --dry-run motion home-all
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock --json --confirm motion home-all
.venv/bin/cli-device --bundle jubilee --target in_memory --socket /tmp/cli-device-demo.sock sys status
kill -TERM "$DPID" && wait "$DPID"
```
结果：**全部退出码 `0`**，`dry-run` 返回 `plan` 且 `homed=[]`，`--confirm` 后 `homed=["U","X","Y","Z"]`，普通文本 `sys status` 输出 `[OK] sys.status (target=in_memory, mode=live, 0ms)`，`SIGTERM` 干净退出。

### 2.2 README Quickstart #2（现起草 + 编译 `scpi_psu` 夹具）实跑
```bash
.venv/bin/cli-device bundle draft \
  --name bench_psu --display-name "Synthetic SCPI PSU Fixture" --kind instrument \
  --interface tcp_scpi --endpoint tcp://192.168.10.50:5025 \
  --manual tests/fixtures/scpi_psu/manual.md \
  --host-api tests/fixtures/scpi_psu/psu_host.py \
  --output /tmp/bench_psu_bundle
.venv/bin/cli-device bundle validate /tmp/bench_psu_bundle
.venv/bin/cli-device bundle build /tmp/bench_psu_bundle --output-dir /tmp/bench_psu_gen
.venv/bin/cli-device bundle build /tmp/bench_psu_bundle --output-dir /tmp/bench_psu_gen --check
.venv/bin/cli-device --bundle /tmp/bench_psu_bundle --help
```
结果：**全部通过**，`--help` 正确显示 `Device Commands (bench_psu):`（`sys`、`instrument`、`expert`）与 `Toolkit Commands:`。

### 2.3 帮助与错误处理、测试、静态检查
- `.venv/bin/cli-device --help` / `--bundle jubilee --help` / `--bundle jubilee motion --help` / `cli-device-d --help`：均显示精确口号与清晰分组。
- `.venv/bin/cli-device sys detect`（未传 `--bundle`）：退出码 `2`，提示 `Pass --bundle <name|dir> ... or run 'cli-device bundle list'`。
- `.venv/bin/cli-device-d --bundle nonexistent_bundle`：退出码 `2`，干净输出 `Error: Bundle 'nonexistent_bundle' not found ...`（无 traceback）。
- `.venv/bin/pytest -v`：**54 passed, 1 skipped**（唯一跳过为 `.venv` 未装 `science-jubilee` 的合规用例）。
- `.venv/bin/mypy`：**Success: no issues found in 23 source files**。
- `.venv/bin/ruff check core_types adapter_spi compiler safety_kernel daemon cli tests/test_import_dag.py tests/test_contracts_and_spi.py tests/test_compiler.py tests/test_safety_kernel.py tests/test_daemon_and_cli.py tests/test_framework_fixes_and_fixtures.py`：**All checks passed!**
- `.venv/bin/cli-device bundle build jubilee --check`：无漂移通过。

---

## 3. 公开页面视觉资源与真实终端 GIF 录制（本轮追加）

### 3.1 新增视觉资产（全部位于 `docs/assets/`，`README.md` 均用相对路径引用）
| 文件 | 用途与说明 |
|---|---|
| `docs/assets/hero-banner.svg` / `docs/assets/hero-banner.png` | GitHub 顶部 Hero 横幅（1280×420，深色 slate 现代开源风，保留精确口号 `Let AI drive all your devices`；右侧为通用 `Serial / HTTP / SCPI / Python → device.yaml + catalog.yaml → SafetyKernel` 示意图，不把 Jubilee 画成产品本身，无廉价插画/假真机图）。由无头 Chrome 渲染 `DM Sans` + `DM Mono` 保证跨平台字体一致，并已目视核查边缘留白。 |
| `docs/assets/architecture.svg` / `docs/assets/architecture.png` | 构建期与运行期分层架构图（1200×310，含 `ActionDriver` 常规路径与虚线 `Lock-Free Abort` 锁外急停路径，强调不改原厂固件）。 |
| `docs/assets/record_demo.py` | 可复现录制脚本：拉起真实 `.venv/bin/cli-device-d`，依次通过 `subprocess.run` 执行真实 `.venv/bin/cli-device` 命令，捕获原始标准输出与退出码，再用 Pillow 渲染终端帧并经 `ffmpeg`（`palettegen`/`paletteuse`）编码为循环 GIF，最后向守护进程发 `SIGTERM` 干净退出。零伪造输出。 |
| `docs/assets/demo-transcript.json` | 本次录制采集的 7 条真实命令及其原始 stdout 与退出码审计记录。 |
| `docs/assets/demo-terminal.gif` | 最终终端演示动图（940×540，201.3 KiB），完整演示：`cli-device --help` → `cli-device --bundle jubilee --help` → `sys detect` → `--dry-run motion home-all` → 未带 `--confirm` 被拒绝（`E_CONFIRM_REQUIRED`）→ `--confirm motion home-all` → `--dry-run sys abort`。 |

### 3.2 `cli/main.py` 细节微调
- 优化普通文本模式下 `sys.detect` 的 `identity` 输出（展示为紧凑单行 `identity: api=... endpoint=... firmware=...`），使人工阅读与演示 GIF 均更清晰；`--json` 输出格式完全不变。

### 3.3 录制与回归验证命令
```bash
/usr/bin/python3 docs/assets/record_demo.py
.venv/bin/pytest tests/test_daemon_and_cli.py tests/test_framework_fixes_and_fixtures.py tests/test_import_dag.py tests/test_compiler.py tests/test_safety_kernel.py tests/test_contracts_and_spi.py -v
.venv/bin/mypy
.venv/bin/ruff check core_types adapter_spi compiler safety_kernel daemon cli tests/test_import_dag.py tests/test_contracts_and_spi.py tests/test_compiler.py tests/test_safety_kernel.py tests/test_daemon_and_cli.py tests/test_framework_fixes_and_fixtures.py
.venv/bin/cli-device bundle build jubilee --check
```
- `record_demo.py`：成功捕获 7 条真实命令并生成 `docs/assets/demo-terminal.gif`（201.3 KiB）；已通过图像读取工具逐张核查 `hero-banner.png`、`architecture.png` 与 GIF 关键帧，无裁切、无重叠。
- `pytest`：**32 passed**；`mypy --strict`：23 文件 **0 错误**；`ruff check`：**0 错误**；`bundle build jubilee --check`：无漂移通过。
