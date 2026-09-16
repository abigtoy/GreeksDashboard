"""
watchdog.py — GreeksDashboard 看门狗

职责：监控服务存活，进程崩溃时自动拉起。
仅在进程真正消失时行动，不介入正常断线重连（CTP 自身10次重试已处理）。

防堵死机制：
  - 每次拉起后 cooldown 30s，期间不再重启
  - 每小时最多重启 3 次，超限后静默放弃，等人工干预
  - 启动前用 tasklist 确认旧进程真死了
  - 不 kill 旧进程
"""

import os
import sys
import socket
import subprocess
import time
import json
import ctypes
import ctypes.wintypes as wintypes
from pathlib import Path

# ── 项目路径 ──────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.resolve()
_PYTHON_PATH  = "C:/veighna_studio/python.exe"
_RUN_SCRIPT   = _PROJECT_ROOT / "run_server.py"
_PORT         = 5000
_COOLDOWN_SEC = 30       # 两次重启间隔
_HOUR_SEC     = 3600
_MAX_RESTARTS = 3         # 每小时最多重启次数
_MARKER_FILE  = _PROJECT_ROOT / "watchdog_marker.json"
_PID_FILE     = _PROJECT_ROOT / "watchdog.pid"


# ── Windows API ───────────────────────────────────────────────────────────────
_KERNEL32  = ctypes.WinDLL("kernel32", use_last_error=True)
_TASKLIST   = "tasklist /FI \"PID eq {pid}\" /NH"

_GetTickCount64 = _KERNEL32.GetTickCount64
_GetTickCount64.restype = ctypes.c_uint64


def _get_tick64() -> int:
    return _GetTickCount64()


def _read_marker() -> dict:
    if _MARKER_FILE.exists():
        try:
            return json.loads(_MARKER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"restarts": [], "last_start": 0, "watchdog_pid": os.getpid()}


def _write_marker(m: dict):
    _MARKER_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _port_listening() -> bool:
    """检查端口是否在监听"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return sock.connect_ex(("127.0.0.1", _PORT)) == 0
    finally:
        sock.close()


def _process_alive(pid: int) -> bool:
    """用 tasklist 确认指定 PID 是否存活"""
    try:
        out = subprocess.check_output(
            _TASKLIST.format(pid=pid),
            stderr=subprocess.DEVNULL,
            text=True,
            shell=True,
        )
        # 输出示例: "    1234 Console     1        1234 ..."  有数字则存活
        return str(pid) in out
    except Exception:
        return False


def _start_server() -> int | None:
    """启动服务，返回新进程 PID；失败返回 None"""
    try:
        proc = subprocess.Popen(
            [_PYTHON_PATH, str(_RUN_SCRIPT)],
            cwd=str(_PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return proc.pid
    except Exception:
        return None


def _load_watchdog_pid() -> int | None:
    """读取看门狗自身 PID"""
    if _PID_FILE.exists():
        try:
            return int(_PID_FILE.read_text().strip())
        except Exception:
            pass
    return None


def _save_watchdog_pid():
    _PID_FILE.write_text(str(os.getpid()))


# ── 主循环 ───────────────────────────────────────────────────────────────────
def run():
    _save_watchdog_pid()

    # 启动时先等待 cooldown（防止刚手动重启又被拉起）
    marker = _read_marker()
    last_start = marker.get("last_start", 0)
    elapsed = (_get_tick64() - last_start) / 1000.0
    if elapsed < _COOLDOWN_SEC:
        print(f"[看门狗] 启动，等待 {int(_COOLDOWN_SEC - elapsed)}s cooldown...")
        time.sleep(_COOLDOWN_SEC - elapsed)

    print("[看门狗] 启动完成")

    while True:
        time.sleep(10)  # 每 10s 检查一次

        if _port_listening():
            continue  # 服务活着，不动

        marker = _read_marker()
        now_ms  = _get_tick64()

        # 清理一小时外的旧记录
        cutoff = now_ms - (_HOUR_SEC * 1000)
        marker["restarts"] = [t for t in marker["restarts"] if t > cutoff]

        # 超过重试上限
        if len(marker["restarts"]) >= _MAX_RESTARTS:
            print(f"[看门狗] ⚠️  已达每小时上限({_MAX_RESTARTS}次)，停止自动重启，等人工干预")
            # 写一个标志，等人工干预后删除
            (_PROJECT_ROOT / "watchdog_giveup.flag").write_text(
                json.dumps({"ts": time.time(), "restarts": marker["restarts"]}),
                encoding="utf-8",
            )
            break

        # cooldown 检查
        last_start = marker.get("last_start", 0)
        if (now_ms - last_start) < _COOLDOWN_SEC * 1000:
            continue

        # 确认旧进程真死了
        old_pid = marker.get("server_pid")
        if old_pid and _process_alive(old_pid):
            print(f"[看门狗] PID {old_pid} 仍存活，等待...")
            continue

        print(f"[看门狗] 🚀 进程消失，尝试拉起...")
        new_pid = _start_server()
        if new_pid is None:
            print("[看门狗] ❌ 启动失败，等待下次检测...")
            time.sleep(_COOLDOWN_SEC)
            continue

        marker["restarts"].append(now_ms)
        marker["last_start"] = now_ms
        marker["server_pid"] = new_pid
        _write_marker(marker)

        print(f"[看门狗] ✅ 已拉起 PID={new_pid}，等待 {_COOLDOWN_SEC}s")
        time.sleep(_COOLDOWN_SEC)


if __name__ == "__main__":
    # 防止重复启动看门狗自身
    my_pid = os.getpid()
    existing = _load_watchdog_pid()
    if existing and _process_alive(existing) and existing != my_pid:
        print(f"[看门狗] 看门狗已在 PID={existing} 运行，拒绝重复启动")
        sys.exit(1)

    try:
        run()
    except KeyboardInterrupt:
        print("[看门狗] 退出")
