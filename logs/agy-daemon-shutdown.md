# `cli-device-d` 信号退出卡死复现、修复与验证记录

- **日期：** 2026-11-21
- **基线 HEAD：** `ddb4e0a`
- **涉及文件：** `daemon/main.py`、`tests/test_daemon_and_cli.py`（未修改 `adapters/*`、`tests/adapter_compliance/*` 与 `/workspace/autodna_sim_*`）。

---

## 1. 复现与根因核实

在 `ddb4e0a` 上启动真实子进程 `python -m daemon.main --bundle jubilee --socket <tmp>/repro.sock`，待 socket 创建后发送 `SIGTERM` 并 `wait(timeout=2.0)`：

```text
REPRODUCED HANG: still alive after 2.000s, sock_exists=True
```

**核实根因（`daemon/main.py`）：**
- 原实现在主线程运行 `server.serve_forever()`，且在 `signal.SIGINT` / `signal.SIGTERM` 回调 `_handle_sig` 中直接调用 `server.shutdown()`。
- Unix 下 Python 信号处理器在**主线程**执行；而 `socketserver.BaseServer.shutdown()` 会置位 `__shutdown_request` 后阻塞等待 `self.__is_shut_down.wait()`，该事件只有 `serve_forever()` 返回时才会触发。
- 因此主线程在 `serve_forever()` 内被信号打断后于同一线程调用 `server.shutdown()`，形成永久自死锁，socket 文件也无法走到 `finally: server.server_close()` 清理。

---

## 2. 修复方案

修改 `daemon/main.py`：
1. 将 `server.serve_forever(poll_interval=0.05)` 放到后台线程 `serve_thread` 中运行。
2. 信号处理器 `_handle_sig` 仅调用 `stop_event.set()`（不阻塞、不拿 `server.shutdown()` 锁）。
3. 主线程循环等待 `stop_event.wait(timeout=0.1)`；收到 `SIGTERM` 或 `SIGINT` 后立即跳出进入 `finally`，在主线程调用 `server.shutdown()` → `serve_thread.join(timeout=2.0)` → `server.server_close()`（关闭监听 socket、释放 `DeviceDaemon` 并 `os.unlink(socket_path)`），随后恢复原信号处理器并以退出码 `0` 正常结束。
4. 启动打印后调用 `sys.stdout.flush()`，避免管道模式下日志缓冲。

---

## 3. 回归测试与验证结果

在 `tests/test_daemon_and_cli.py` 新增真实子进程参数化回归测试 `test_cli_device_d_exits_cleanly_and_unlinks_socket_on_signal`（覆盖 `SIGTERM` 与 `SIGINT`）：
- 启动 `python -m daemon.main --bundle bundles/jubilee --socket <tmp_sock>`；
- 轮询至 socket 存在且真实 `DaemonIPCClient.call(target="in_memory", service="sys.status")` 返回 `ok=True`；
- 发送信号，要求 `proc.wait(timeout=1.5)` 在 `< 1.0s` 内以 `returncode == 0` 退出，并断言 `not socket_path.exists()`；`finally` 保证清理无僵尸进程。

**实测输出：**
- 连续 5 轮压测 `test_cli_device_d_exits_cleanly_and_unlinks_socket_on_signal`：**5/5（10/10 用例）全部通过**。
- 真实子进程耗时实测：
  - `VERIFIED SIGTERM: rc=0, elapsed=63.5ms, sock_exists=False`
  - `VERIFIED SIGINT: rc=0, elapsed=63.5ms, sock_exists=False`
- 相关套件校验：
  - `.venv/bin/pytest tests/test_daemon_and_cli.py tests/test_framework_fixes_and_fixtures.py -v`：**12 passed in 7.31s**（原框架修复与夹具测试全部保持通过）。
  - `.venv/bin/mypy`：**Success: no issues found in 23 source files**。
  - `.venv/bin/ruff check daemon/main.py tests/test_daemon_and_cli.py`：**All checks passed!**
