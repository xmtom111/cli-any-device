# Antigravity 框架缺陷修复与通用验证夹具报告

- **日期：** 2026-11-21
- **基线 HEAD：** `b427331`
- **口号：** `Let AI drive all your devices`
- **范围约束：** 仅修改我方模块（`pyproject.toml`、`adapter_spi/`、`compiler/`、`safety_kernel/`、`daemon/`、`docs/generated/` 重编译、`tests/` 与 `tests/fixtures/`），未修改 `adapters/*`、`tests/adapter_compliance/*` 与 `/workspace/autodna_sim_*`，未对真机发运动。

---

## 1. 六项框架缺陷修复明细

| # | 缺陷 | 修复方式 | 回归验证命令 | 结果 |
|---|---|---|---|---|
| 1 | `--dry-run sys abort` 走 `bypasses_action_lock` 分支时实际触发并闩锁了急停 | 在 `safety_kernel/engine.py` 的 `service.bypasses_action_lock` 分支中显式处理 `dry_run=True`：不调用 `supervisor.abort_now()`，无锁返回 `mode=dry_run` + `data["plan"]`；`dry_run=False` 仍走锁外真实急停。 | `.venv/bin/pytest tests/test_framework_fixes_and_fixtures.py -k test_dry_run_sys_abort_returns_plan_without_latching_estop -v` | **PASS**（dry-run 返回 plan 且 `abort_count==0`、`estop_latched==False`、`homed` 保留；live abort 行为不变） |
| 2 | 手册限值键名（如 `receive_frequency_range`）未绑到运行时参数名（如 `frequency_mhz`），导致越界调频/发信放行 | 在 `compiler/ingestor.py` 新增 `_bind_limits_to_service_args()`：按语义词干 + 单位（MHz/W/dB/V/A/mm/rpm/mm/min 等）将手册范围绑定到匹配的服务数值参数名，同时写入 `device.yaml` 的 `safety_limits[arg_name]` 与 `catalog.yaml` 对应 `args[*]` 的 `min_value`/`max_value`/`unit`。 | `.venv/bin/pytest tests/test_framework_fixes_and_fixtures.py -k test_open_radio_limits_bind_to_runtime_args_and_reject_out_of_bounds -v` | **PASS**（`open_radio` 生成 `frequency_mhz`/`power_w`/`volume_db`；越界 `150 MHz` 与 `12 W` 均在驱动调用前以退出码 `3` / `E_OUT_OF_BOUNDS` 拒绝） |
| 3 | Python 上位机 keyword-only 参数（如 `def read_probe(*, channel: int)`）被 `bundle draft` 丢弃 | 在 `compiler/ingestor.py` 新增 `_iter_signature_params()`，按序提取 `posonlyargs`、`args` 与 `kwonlyargs`（含 `kw_defaults`），全部生成 catalog `args` 与动态 CLI `--<arg>` 选项。 | `.venv/bin/pytest tests/test_framework_fixes_and_fixtures.py -k test_scpi_psu_fixture_kwonly_args_limits_and_cli_closed_loop -v` | **PASS**（`instrument.read_probe` 生成必填 `channel: int` 与 CLI `--channel`，且与手册限值 `1..4` 绑定） |
| 4 | 生成的 `SKILL.md` 缺少 `--bundle` / `cli-device-d` / 插件安装前置说明，且对 `in_memory` 误写「命令等到物理完成」 | 更新 `compiler/builder.py:render_skill_md()`：明确写出 `--bundle <id>`、`cli-device-d --bundle <id>`、需要安装/注册 `cli_any_device.adapters` 的目标列表、`--target in_memory` 为离线内存假实现（无物理动作），仅对非 `in_memory` 目标说明 `await_settled` 完成语义；并通过 `cli-device bundle build jubilee` 重编译 `docs/generated/jubilee/`。 | `.venv/bin/pytest tests/test_framework_fixes_and_fixtures.py -k test_generated_skill_documents_prerequisites_and_distinguishes_in_memory -v && .venv/bin/cli-device bundle build jubilee --check` | **PASS**（`docs/generated/jubilee/SKILL.md` 无漂移且仅 `in_memory` 设备包不再声称物理完成） |
| 5 | 非 RRF/Duet 设备草稿默认将 `fail_safe` 写成 `abort_m112`，泄漏 RRF 术语 | 在 `compiler/ingestor.py` 新增 `_default_fail_safe()`：仅 RRF/Duet 接口用 `abort_m112`，其余射频/仪表/通用设备使用设备无关策略 `disable_output` / `adapter_abort`；同时在 `adapter_spi/fake.py:apply_fail_safe()` 支持 `adapter_abort` / `halt_motion` / `safe_halt` / `disable_output`。 | `.venv/bin/pytest tests/test_framework_fixes_and_fixtures.py -v` | **PASS**（`open_radio`、`grbl_engraver`、`scpi_psu` 草稿的 `catalog.yaml` 零 `abort_m112`） |
| 6 | 根 `.venv` 的 `cli_any_device.adapters` `entry_points` 为空，导致 `cli-device-d` 无法解析 `mujoco_twin` / `grbl_sim` / `duet_real` | 在根 `pyproject.toml` 注册 `[project.entry-points."cli_any_device.adapters"]`（`duet_rrf`、`mujoco_twin`、`grbl_sim`），在 `daemon/registry.py` 改为按需懒加载 entry points，并在 `.venv` 执行 `uv pip install --python .venv/bin/python --no-deps -e .`。 | `.venv/bin/pytest tests/test_framework_fixes_and_fixtures.py -k test_root_package_exposes_adapter_entry_points_and_daemon_resolves_targets -v` | **PASS**（`.venv` 可直接发现 3 个适配器入口，`DeviceDaemon` 对 `mujoco_twin` / `grbl_sim` / `duet_real` 均可解析，不再报 `E_UNKNOWN_TARGET`） |

---

## 2. 新增通用开源硬件验证夹具（离线）

保留原有 `tests/fixtures/open_radio/` 与 `bundles/jubilee/`，新增两套更简单、常见的开源硬件测试夹具：

1. **三轴 GRBL 桌面雕刻机**（`tests/fixtures/grbl_engraver/{manual.md,engraver_host.py}`）
   - USB 串口（`serial:///dev/ttyUSB0`），无换刀机构；覆盖 `home_axes`（R3 `--confirm`）、关键字参数 `move_xyz(*, x_mm, y_mm, z_mm, feed_rate=600.0)`、`set_spindle(*, spindle_rpm)` 与专家逃逸 `send_raw_gcode`。
   - 完整闭环测试（`bundle draft -> bundle build --check -> bundle validate -> daemon + dynamic CLI`）：验证 `x_mm`/`y_mm`/`z_mm`/`spindle_rpm`/`feed_rate` 限值绑定、越界退出码 `3`、`--dry-run sys abort` 不闩锁、无 `abort_m112`。
2. **SCPI 程控直流电源 + 温度探头**（`tests/fixtures/scpi_psu/{manual.md,psu_host.py}`）
   - `tcp_scpi` 接口（`tcp://192.168.10.50:5025`），纯非运动仪表；覆盖 `read_probe(*, channel: int)`、`get_measurements`、`set_voltage`、`set_current_limit`、`enable_output` 与 `send_raw_scpi`。
   - 完整闭环测试：验证 keyword-only `--channel` 生效与 `1..4` 限值拦截、`voltage_v`（0–30 V）与 `current_a`（0–5 A）越界退出码 `3`、`enable-output` / `expert send-raw-scpi` 需 `--confirm`（退出码 `4`）、`SKILL.md` 隐藏专家命令并写出前置步骤、无 `abort_m112`。

---

## 3. 全量校验结果

```bash
.venv/bin/cli-device bundle build jubilee --check
.venv/bin/pytest -v
.venv/bin/mypy
.venv/bin/ruff check core_types adapter_spi compiler safety_kernel daemon cli tests/test_import_dag.py tests/test_contracts_and_spi.py tests/test_compiler.py tests/test_safety_kernel.py tests/test_daemon_and_cli.py tests/test_framework_fixes_and_fixtures.py
```

- `pytest -v`：**52 passed, 1 skipped**（唯一跳过仍为 Codex 合规套件中因 `.venv` 未装 `science-jubilee` 而跳过的 `test_duet.py::test_pinned_vendor_rehearsal_captures_frames`）。
- `mypy --strict`：23 个源文件 **0 错误**。
- `ruff check`：**0 错误**。
- `cli-device bundle build jubilee --check`：无漂移通过。
- 未修/待后续项：无本轮范围内遗留缺陷；`science-jubilee` 离线排练与真机硬件验证仍属外部依赖/物理真机前提。
