# CLI-Any-Device 交叉验收

Let AI drive all your devices

**结论：交叉验收未通过。** 通用离线链路成立，但生成包未执行手册范围，且 dry-run 急停会执行。实测基线为 `2ddbbdc`，使用项目 `.venv` 的 Python 3.13.5 和 MuJoCo 3.3.7。Jubilee 用于验证运动与安全行为，AutoDNA 提供仿真参考。本轮只使用内存、仿真和环回夹具。

## 1. 接口、手册与上位机到动态 CLI

从仓库根目录执行。复现命令把生成物写到临时目录，实测默认输出目录为 `docs/generated/open_radio`。

```sh
C=.venv/bin/cli-device
F=tests/fixtures/open_radio
T=$(mktemp -d /tmp/cadx.XXXXXX)
"$C" bundle draft --name open_radio --display-name OpenRadio-100 --kind radio \
  --interface usb_serial --endpoint serial:///dev/ttyUSB0 \
  --manual "$F/manual.md" --host-api "$F/radio_host.py" --output "$T/radio"
"$C" bundle build "$T/radio" --output-dir "$T/generated"
"$C" bundle validate "$T/radio"
"$C" bundle build "$T/radio" --output-dir "$T/generated" --check
.venv/bin/cli-device-d --bundle "$T/radio" --socket "$T/s" &
D=$!
# 等待 test -S "$T/s" 成功再执行。
radio() { "$C" --bundle "$T/radio" --target in_memory --socket "$T/s" --json "$@"; }
radio --help
radio sys detect
radio sys preflight
radio radio tune --frequency-mhz 101.5
"$C" --bundle "$T/radio" --target hardware_real --socket "$T/s" --json sys detect
```

| 声称 | 结果 | 实测依据 |
|---|---|---|
| 接口、手册与 Python 上位机能生成设备包草稿 | 通过，限夹具所覆盖的输入 | 标记 `review_status=draft`，生成两份 YAML。提取六个域命令和手册中的三个范围，并保留接线与安全提示。 |
| draft → build → validate → daemon + 动态 CLI | 通过，限离线适配器 | 三条 bundle 命令退出 0，validate 报告 `11 services, 2 targets`，build --check 报告 `Verified (no drift)`。CLI 帮助列出 `radio` 和 `expert`，detect/preflight 退出 0，调谐返回 `properties.frequency_mhz=101.5`。 |
| 从接口和手册直接得到可控设备 | 失败，硬件接通未证明 | 同一 draft 命令删去 `--host-api` 并换输出目录后仍可 build/validate，但只剩五个 `sys.*` 命令。完整草稿的 `hardware_real sys detect` 退出 2，`E_UNKNOWN_TARGET`，显示 `open_radio_adapter` 未安装或注册。 |
| Python 接口签名可直接成为完整 CLI 参数 | 失败 | 用下述独立温度探头夹具。AST 提取读数命令且跳过模块顶层异常，然而必需的关键字参数被丢掉。 |

第二个夹具的最小复现如下。

```sh
cat > "$T/sensor.py" <<'PY'
raise RuntimeError("AST input must never execute")
def read_probe(*, channel: int) -> float:
    """Read a probe on the required channel."""
    raise NotImplementedError
PY
"$C" bundle draft --name probe --display-name Probe --kind thermometer \
  --interface tcp_scpi --endpoint tcp://127.0.0.1:9999 \
  --host-api "$T/sensor.py" --output "$T/probe"
"$C" bundle build "$T/probe" --output-dir "$T/probe-gen"
"$C" bundle validate "$T/probe"
grep -A8 'thermometer.read_probe' "$T/probe/catalog.yaml"
"$C" --bundle "$T/probe" thermometer read-probe --help
```

validate 报 `6 services, 2 targets`，目录却显示 `args: []`，帮助只有 `--help`，未生成 `--channel`。提取器目前只遍历普通位置参数 `fn.args.args`。

## 2. 文档验收

- **通过，篇幅与目录。** `wc -lw "$T/generated/"{SKILL,HOOKUP}.md` 实测 SKILL 38 行 / 263 词，HOOKUP 59 行 / 459 词。SKILL 列出动态域命令、退出码、急停，省略原始 AT 命令。HOOKUP 列出 USB 串口、115200 波特率、天线或假负载，以及未现场验证的范围。对两份 Markdown 搜索 `jubilee|autodna|duet|grbl|m112` 无匹配。
- **失败，仅凭文档启动。** 把 SKILL 示例中的目标替换为实际的 `in_memory`，运行 `"$C" --json --target in_memory sys detect`，退出 2，`No such command 'sys'`。增加 `--bundle "$T/radio"` 和对应 socket 后才复现离线闭环。SKILL 没有交代加载 bundle、启动 daemon 或适配器安装的前置步骤。
- **失败，完成语义描述过宽。** SKILL 声称命令等到物理完成，实测 detect 身份为 `in_memory_fake`。调谐成功只证明内存属性更新。HOOKUP 中的 TX 静音来自手册摘录，真机行为未证明。

## 3. 安全门与完成语义

以下命令复用上面的 `radio` 函数。验证 dry-run 急停时重启 daemon，让初始状态为 IDLE。

| 声称 | 结果 | 命令与关键输出 |
|---|---|---|
| 普通动作 dry-run 不执行 | 通过 | `radio sys status` → `radio --dry-run radio tune --frequency-mhz 101.5` → `radio sys status`。返回 `mode=dry_run`、计划参数 101.5，前后属性均为 `{}`。MuJoCo 集成用例还断言零 driver 调用。 |
| 所有 dry-run 都不执行 | 失败，运行时 | `radio sys status` 为 `IDLE/false`。执行 `radio --dry-run sys abort`，实际返回 `mode=dry_run, sent=true, HALTED, estop_latched=true`。之后 `sys status` 仍为 HALTED。内核在 dry-run 判断前进入 `bypasses_action_lock` 分支。复现只使用内存目标。 |
| 缺确认拒绝 | 通过 | `radio radio enable-tx --power-w 1` 和 `radio expert send-raw-at --command AT+VER` 都退出 4，`E_CONFIRM_REQUIRED`，拒绝时属性未变化。 |
| 草稿把手册限值变成有效门控 | 失败，草稿生成 | `radio radio tune --frequency-mhz 200` 及 `radio --confirm radio enable-tx --power-w 6` 都退出 0，并更新模拟属性。手册范围为 87.5–108 MHz 和 0–5 W。生成的键为 `receive_frequency_range` / `transmit_power`，运行时按 `frequency_mhz` / `power_w` 精确查找。在临时副本给两条限值加上与参数同名的键，重建并换 daemon 后，同样两条命令都退出 3，`E_OUT_OF_BOUNDS`，步骤 `safety_limit_check`。 |
| GRBL 换刀诚实失败 | 通过 | 合规测试调用取刀、停刀、锁、解锁和 T 宏，断言 `E_UNSUPPORTED_CAPABILITY` 且无协议帧。内核集成测试也断言取刀未进入 driver。 |
| 急停锁外 | 通过，适配器与独立进程 IPC | `test_second_process_aborts_grbl_diagonal` 在运动中用第二进程发送 abort，断言通道耗时低于 100 ms、位置稳定且回原点失效。Duet 独立会话测试和 MuJoCo 阻塞几何测试通过。内存 OpenRadio 的 live abort 之后再调谐退出 99，`E_ESTOP_LATCHED`。 |
| Duet HTTP 200 即完成 | 该行为被测试拒绝 | 独立模式夹具在 HTTP 已响应、甚至已报告 idle 时仍等屏障消息。无关回复不能放行。SBC 同步代码回复后仍需 idle。真实传输、实体停机与硬件时延未证明。 |

复现结束后在启动 daemon 的 shell 执行 `kill "$D"` 或显式停止前台 daemon。

## 4. 设备无关性代码审查

- **通过，共享运行时的设备选择与分派。** `tests/test_import_dag.py` 实测通过。阅读运行时的插件注册、profile/driver 能力交集和派发路径，未发现按 OpenRadio、Jubilee、GRBL、Duet 或 AutoDNA 的设备名选择动作实现。OpenRadio 的命令来自生成 schema，运行时实际通过同一 daemon 分派。
- **部分通过，内核扩展点。** 运动学、工具与载架字段和谓词仍在公共内核。`safety_kernel/predicates.py::resolve_target_coords` 直接匹配 `motion.move_well`、`motion.move_safe`、`motion.move_to`、`motion.move_rel`。这允许非空间设备共享运行时，但另一套空间动词能自动获得相同门控尚未证明。
- **失败，drafter 的协议中立性。** 对 OpenRadio 生成目录和 `compiler/ingestor.py` 搜索 `abort_m112`，命中 `sys.reset_estop`、`radio.calibrate_frontend`、`expert.send_raw_at`。内存 fake 把该策略当成通用停止处理，掩盖草稿中的 RRF 策略命名。实际 radio adapter 未生成，不能据此证明射频设备可处理该策略。

## 5. 本地测试记录和修改范围

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -ra
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -ra tests/adapter_compliance
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q \
  tests/test_daemon_and_cli.py::test_concurrent_ipc_abort_unblocks_deadlocked_action
.venv/bin/ruff check adapters/{duet_rrf,mujoco_twin,grbl_sim} tests/adapter_compliance
.venv/bin/ruff format --check adapters/{duet_rrf,mujoco_twin,grbl_sim} tests/adapter_compliance
```

整仓最终一轮 `1 failed, 41 passed, 1 skipped`。适配器合规 `20 passed, 1 skipped`。项目 venv 无可选的 science-jubilee，本轮离线厂商排练未证明。Ruff 检查和格式检查通过。

已知并发 CLI 测试在整仓复现失败，随后单独五次重跑为 `FAIL PASS PASS PASS PASS`。失败时断言收到 `payload["ok"] == False`，与此同时捕获 stdout 有成功的 `sys.abort` JSON，后台线程还报 `JSONDecodeError`。该现象与同进程并发 Click CliRunner 捕获输出串扰相符，尚未证明根因。独立进程的适配器 IPC 急停测试通过。这项 flaky 留给 Antigravity。

本轮只删除适配器重复导入、Duet 未消费的内部遥测键与重复解释。保留完成屏障、独立 M112、工具拒绝和物理动作禁用。编译器、CLI、内核与 daemon 缺陷仅记录。AutoDNA 资产保持只读。
