# 适配器框架修复与交叉复测

Let AI drive all your devices

起点 `b427331`。框架复测对应 Antigravity 提交 `7314e86`。使用项目 `.venv` 和 MuJoCo 3.3.7。Jubilee 仅用来验证三个随根包安装的插件，另用 OpenCNC-3018 草稿验证三轴运动。所有动作均在内存、仿真或本地 HTTP 夹具执行。

## 根包与适配器

```sh
uv pip install --python .venv/bin/python --no-deps --reinstall -e .
.venv/bin/python -c 'from importlib.metadata import entry_points; print(entry_points(group="cli_any_device.adapters"))'
uv build --wheel --out-dir "$(mktemp -d /tmp/cadwheel.XXXXXX)"
```

| 检查 | 结果与关键输出 |
|---|---|
| 默认安装加载插件 | 通过。entry point 列出 `duet_rrf`、`grbl_sim`、`mujoco_twin`，三个工厂可加载。检查根 wheel，确认三个模块与同组入口均在包内。 |
| 实际 daemon 默认发现目标 | 通过。启动 `.venv/bin/cli-device-d --bundle jubilee --socket "$S"`，分别运行 `.venv/bin/cli-device --bundle jubilee --socket "$S" --target "$target" --json sys detect`。三个目标均退出 0。MuJoCo、GRBL 为 `detected=true, IDLE`。 |
| Duet 离线 detect 自洽 | 通过。进程未配置 `CLI_ANY_DEVICE_DUET_PASSWORD`。CLI 返回 0、`detected=true, DISCONNECTED`，Identity 包含 `offline=true, read_only=true`。首次 detect 之前仍为 `detected=false`。经 daemon 提交确认过的回原点请求，能力门拒绝。 |
| 完成和急停回归 | 通过。适配器套件覆盖独立 HTTP 会话的 M112、HTTP 已响应但屏障尚未完成、SBC 完成需 idle，以及 GRBL 换刀拒绝和独立客户端急停。真机 M112 及物理时延未证明。 |

`test_engraver_draft_dispatches_through_grbl_emulator` 直接复用 `tests/fixtures/grbl_engraver` 的手册和上位机。测试显式绑定草稿的两个 XYZ 命令，保留原来的参数名，再走默认的内核门控和真实 GRBL 模拟控制器。缺确认回原点被拒绝，确认后回原点和 `move_xyz` 成功，最终位置为 `X=6, Y=7, Z=5`。超过手册上限的 X 被拒绝，主轴命令因能力不足拒绝。低于仿真安全 Z 的 XY 移动触发错误，通用 `adapter_abort` 经实时复位闩锁模拟器。真机串口和主轴控制未证明。

## 框架独立复测

```sh
C=.venv/bin/cli-device
F=tests/fixtures/open_radio
T=$(mktemp -d /tmp/cadfw.XXXXXX)
"$C" bundle draft --name open_radio --display-name OpenRadio-100 --kind radio \
  --interface usb_serial --endpoint serial:///dev/ttyUSB0 \
  --manual "$F/manual.md" --host-api "$F/radio_host.py" --output "$T/radio"
"$C" bundle build "$T/radio" --output-dir "$T/gen"
"$C" bundle validate "$T/radio"
# 在另一个终端启动，并用同一个 T。
.venv/bin/cli-device-d --bundle "$T/radio" --socket "$T/s"
```

客户端使用 `radio() { "$C" --bundle "$T/radio" --target in_memory --socket "$T/s" --json "$@"; }`。先运行 `radio sys detect`，退出 0。build 和 validate 均退出 0，validate 报告 `11 services, 2 targets`。

| 复测项 | 结果与关键输出 |
|---|---|
| dry-run 急停不闩锁 | 通过。`radio sys status` → `radio --dry-run sys abort` → `radio sys status`。急停 dry-run 退出 0，`mode=dry_run`，只有 `data.plan`。前后均为 `IDLE, estop_latched=false`。 |
| 手册限值落到实际参数 | 通过。新草稿的键为 `frequency_mhz`、`power_w`、`volume_db`。`radio radio tune --frequency-mhz 200` 和 `radio --confirm radio enable-tx --power-w 6` 均退出 3、`E_OUT_OF_BOUNDS`，前后属性仍为空。 |
| 关键字参数进入 CLI | 通过。另以 `def read_probe(*, channel: int) -> float` 生成 `--kind thermometer --interface tcp_scpi` 草稿。build 后执行 `"$C" --bundle "$T/probe" thermometer read-probe --help`，显示 `--channel INTEGER … [required]`。 |
| 非 RRF 草稿无 `abort_m112` | 通过。读取实际 OpenRadio 生成的 catalog，非空策略为 `adapter_abort` 或 `disable_output`。SCPI PSU 测试也在其 detect 步骤前通过对应断言。 |
| SKILL 写明前置步骤 | 通过。OpenRadio 生成的 SKILL 写明 `--bundle open_radio` 或 `--bundle <bundle_dir>`，并给出 `cli-device-d --bundle open_radio --socket /tmp/cli-device-d.sock`。另标记内存目标没有物理动作。临时草稿使用实际目录替换包名。 |

## 测试记录与范围外问题

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -ra tests/adapter_compliance
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -ra
.venv/bin/ruff check adapters tests/adapter_compliance
.venv/bin/ruff format --check adapters tests/adapter_compliance
.venv/bin/cli-device bundle build jubilee --check
```

适配器套件为 **22 通过、1 跳过**，可选 science-jubilee 未安装。Ruff 和 Jubilee 制品无漂移检查通过。首次整仓复测为 **51 通过、1 失败、1 跳过**。并行新增的 SCPI PSU 闭环在 `sys detect` 得到 2，测试工厂引用了当时不存在的 `ApiDialect.SCPI_TCP`。最小复现是 `.venv/bin/python -c 'from core_types import ApiDialect; print(ApiDialect.SCPI_TCP)'`，当时抛出 `AttributeError`。 Antigravity 更新该测试工厂后，整仓重跑为 **52 通过、1 跳过**，SCPI PSU 闭环也通过。

另复现独立 daemon 退出挂起。待其 socket 就绪后发送 `kill -TERM "$D"`，进程在 1 秒内未退出。一次复测等待 10 秒仍超时。当前信号处理函数在执行 `serve_forever` 的线程调用 `server.shutdown()`。仅清理本次启动的测试进程，daemon 实现留给上游处理。
