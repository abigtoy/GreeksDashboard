"""
run_server.py — 启动入口
引用 dashboard_v2.api_server.create_app()
"""

import os
import sys
import socket
import json
import subprocess
import time
import ctypes
import ctypes.wintypes as wintypes

# 项目根目录
_project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _project_root)

from dashboard_v2.api_server import create_app

# 结算单目录（相对于项目根目录）
SETTLEMENT_DIR = os.path.join(_project_root, "结算单")


# ─────────────────────────────────────────────────────────────────────────────
# 单实例互斥锁（Windows Mutex，跨进程有效）
# ─────────────────────────────────────────────────────────────────────────────

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CREATEMUTEXW = _KERNEL32.CreateMutexW
_CREATEMUTEXW.argtypes = [ctypes.c_void_p, wintypes.BOOL, ctypes.c_wchar_p]
_CREATEMUTEXW.restype = wintypes.HANDLE

_WAITFORSINGLEOBJECT = _KERNEL32.WaitForSingleObject
_WAITFORSINGLEOBJECT.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_WAITFORSINGLEOBJECT.restype = wintypes.DWORD

_RELEASE_MUTEX = _KERNEL32.ReleaseMutex
_RELEASE_MUTEX.argtypes = [wintypes.HANDLE]
_RELEASE_MUTEX.restype = wintypes.BOOL

_CLOSE_HANDLE = _KERNEL32.CloseHandle
_CLOSE_HANDLE.argtype = [wintypes.HANDLE]
_CLOSE_HANDLE.restype = wintypes.BOOL

_ERROR_ALREADY_EXISTS = 183

_mtx: wintypes.HANDLE | None = None


def _acquire_lock() -> bool:
    """
    尝试获取 Windows Mutex 单实例锁。
    返回 True 表示获得锁（可以启动），False 表示已有实例在运行。
    """
    global _mtx
    _mtx = _CREATEMUTEXW(None, False, "GreeksDashboard_SingleInstance_Mutex")
    # 如果 mutex 已存在（另一个进程持有），CreateMutexW 仍返回有效句柄，
    # 但 GetLastError() == ERROR_ALREADY_EXISTS；尝试等待0秒看能否获得所有权
    owned = (_WAITFORSINGLEOBJECT(_mtx, 0) == 0)  # 0 = WAIT_OBJECT_0，获得锁
    if not owned:
        _CLOSE_HANDLE(_mtx)
        _mtx = None
        return False
    return True


def _release_lock():
    """退出时释放 Mutex"""
    global _mtx
    if _mtx:
        _RELEASE_MUTEX(_mtx)
        _CLOSE_HANDLE(_mtx)
        _mtx = None


if __name__ == "__main__":
    import atexit as _atexit
    from pathlib import Path as _Path

    # ── 看门狗 marker：记录当前 server PID ───────────────────────────
    _WATCHDOG_MARKER = _Path(_project_root) / "watchdog_marker.json"

    def _write_server_pid():
        try:
            m = {}
            if _WATCHDOG_MARKER.exists():
                m = json.loads(_WATCHDOG_MARKER.read_text(encoding="utf-8"))
            m["server_pid"] = os.getpid()
            _WATCHDOG_MARKER.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _clear_server_pid():
        try:
            if _WATCHDOG_MARKER.exists():
                m = json.loads(_WATCHDOG_MARKER.read_text(encoding="utf-8"))
                m.pop("server_pid", None)
                _WATCHDOG_MARKER.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ── 单实例检查（Mutex，跨进程生效）───────────────────────────────
    if not _acquire_lock():
        print("[拒绝] 服务已在运行，禁止重复启动。")
        sys.exit(1)
    _atexit.register(_release_lock)
    _atexit.register(_clear_server_pid)
    _write_server_pid()

    # ── 端口检查（检查是否有其他进程占着 5000）───────────────────────
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied = sock.connect_ex(("127.0.0.1", 5000)) == 0
    sock.close()
    if occupied:
        print("[启动] 5000 端口被占用，可能是残留连接，继续启动...")

    app = create_app(
        settlement_dir=SETTLEMENT_DIR,
        static_folder=os.path.join(_project_root, "static"),
        template_folder=os.path.join(_project_root, "templates"),
    )
    print(f"启动服务 http://0.0.0.0:5000")
    print(f"结算单目录: {SETTLEMENT_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
