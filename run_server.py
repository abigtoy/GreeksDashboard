"""
run_server.py — 启动入口
引用 dashboard_v2.api_server.create_app()

单实例管理：
- Mutex 互斥（按账户名隔离）
- instance.json 持久化实例信息
- 健康检查区分"已有实例"vs"疑似残留"
- 禁止重复启动，不自动杀进程
"""

import os
import sys
import json
import socket
import ctypes
import ctypes.wintypes as wintypes
BOOL = ctypes.c_long
HANDLE = ctypes.c_void_p
DWORD = ctypes.c_ulong
import time
import urllib.request
import urllib.error

# 项目根目录
_project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _project_root)

from dashboard_v2.api_server import create_app

# 结算单目录
SETTLEMENT_DIR = os.path.join(_project_root, "结算单")

# ─────────────────────────────────────────────────────────────────────────────
# Windows Mutex（跨进程有效）
# ─────────────────────────────────────────────────────────────────────────────

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateMutexW = _KERNEL32.CreateMutexW
_CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, ctypes.c_wchar_p]
_CreateMutexW.restype = wintypes.HANDLE
_WaitForSingleObject = _KERNEL32.WaitForSingleObject
_WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_WaitForSingleObject.restype = wintypes.DWORD
_ReleaseMutex = _KERNEL32.ReleaseMutex
_ReleaseMutex.argtypes = [wintypes.HANDLE]
_ReleaseMutex.restype = wintypes.BOOL
_CloseHandle = _KERNEL32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL
_GetLastError = _KERNEL32.GetLastError
_GetLastError.restype = wintypes.DWORD

_ERROR_ALREADY_EXISTS = 183
_WAIT_TIMEOUT = 0x102

# ─────────────────────────────────────────────────────────────────────────────
# instance.json 管理
# ─────────────────────────────────────────────────────────────────────────────

_INSTANCE_FILE = os.path.join(_project_root, "instance.json")

def _load_instance() -> dict | None:
    """读取已有 instance.json，不存在返回 None"""
    if not os.path.exists(_INSTANCE_FILE):
        return None
    try:
        return json.loads(open(_INSTANCE_FILE, encoding="utf-8").read())
    except Exception:
        return None

def _save_instance(info: dict):
    """原子写入 instance.json"""
    tmp = _INSTANCE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _INSTANCE_FILE)

def _delete_instance():
    """删除 instance.json（忽略不存在）"""
    try:
        if os.path.exists(_INSTANCE_FILE):
            os.remove(_INSTANCE_FILE)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# 健康检查
# ─────────────────────────────────────────────────────────────────────────────

def _health_check(port: int, timeout: float = 2.0) -> dict | None:
    """curl http://127.0.0.1:{port}/api/health，成功返回 JSON，失败返回 None"""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# 获取账户名（用于 Mutex key 和 instance.json）
# ─────────────────────────────────────────────────────────────────────────────

def _get_account_name() -> str:
    """从 ctp_accounts.json 读取当前激活账户名"""
    accounts_file = os.path.join(_project_root, "ctp_accounts.json")
    try:
        data = json.loads(open(accounts_file, encoding="utf-8").read())
        return data.get("active", "default")
    except Exception:
        return "default"

# ─────────────────────────────────────────────────────────────────────────────
# 主逻辑
# ─────────────────────────────────────────────────────────────────────────────

def _build_mutex_name(account: str) -> str:
    return f"Local\\GreeksDashboard_{account}"

def _try_start(port: int, account: str):
    """
    尝试启动服务。
    - 成功：获得 Mutex，写入 instance.json，启动 Flask
    - 失败（已有实例）：打印已有实例信息，退出
    """
    mtx_name = _build_mutex_name(account)
    mtx = _CreateMutexW(None, False, mtx_name)
    owned = (_WaitForSingleObject(mtx, 0) == 0)  # 0 = WAIT_OBJECT_0

    if not owned:
        # Mutex 被占用，尝试健康检查
        _CloseHandle(mtx)
        existing = _health_check(port)
        if existing and existing.get("status") == "running":
            # 已有健康实例，拒绝启动
            inst = existing.get("instance", {})
            print("[拒绝] 服务已在运行，禁止重复启动。")
            print(f"  实例 PID:    {existing.get('pid', 'unknown')}")
            print(f"  账户:        {inst.get('account', 'unknown')}")
            print(f"  启动时间:    {inst.get('started_at', 'unknown')}")
            print(f"  CTP 状态:    {existing.get('ctp_status', 'unknown')}")
            print(f"  健康检查:    http://127.0.0.1:{port}/api/health")
            print()
            print("请先停止当前实例，再启动新实例。")
            print(f"停止命令: curl -X POST http://127.0.0.1:{port}/api/shutdown")
            sys.exit(1)
        else:
            # 疑似残留实例（健康检查失败）
            inst = _load_instance() or {}
            print("[警告] 疑似残留实例（Mute x存在但健康检查失败）。")
            print(f"  记录的 PID:  {inst.get('pid', 'unknown')}")
            print(f"  账户:        {inst.get('account', 'unknown')}")
            print(f"  启动时间:    {inst.get('started_at', 'unknown')}")
            print()
            print("请手动停止残留进程后再启动：")
            if inst.get("pid"):
                print(f"  taskkill /PID {inst['pid']}")
            sys.exit(1)

    # 获得 Mutex，启动服务
    import atexit

    def _cleanup():
        _ReleaseMutex(mtx)
        _CloseHandle(mtx)
        _delete_instance()

    atexit.register(_cleanup)

    # 写入 instance.json
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    instance_info = {
        "instance_id": f"{account}_{int(time.time())}",
        "account": account,
        "pid": os.getpid(),
        "started_at": started_at,
        "port": port,
        "status": "running",
    }
    _save_instance(instance_info)

    print(f"[启动] 账户: {account}")
    print(f"[启动] PID:  {os.getpid()}")
    print(f"[启动] 时间: {started_at}")
    print(f"[启动] 端口: {port}")
    print(f"[启动] 实例文件: {_INSTANCE_FILE}")
    print()

    # 创建 Flask
    app = create_app(
        settlement_dir=SETTLEMENT_DIR,
        static_folder=os.path.join(_project_root, "static"),
        template_folder=os.path.join(_project_root, "templates"),
        instance_info=instance_info,
    )

    # 写 watchdog marker（兼容旧 watchdog）
    _WATCHDOG = os.path.join(_project_root, "watchdog_marker.json")
    try:
        wd = json.loads(open(_WATCHDOG).read()) if os.path.exists(_WATCHDOG) else {}
    except Exception:
        wd = {}
    wd["server_pid"] = os.getpid()
    open(_WATCHDOG, "w", encoding="utf-8").write(json.dumps(wd, ensure_ascii=False))
    atexit.register(lambda: (wd.pop("server_pid", None),
                              open(_WATCHDOG, "w", encoding="utf-8").write(json.dumps(wd, ensure_ascii=False)))
                              if os.path.exists(_WATCHDOG) else None)

    print(f"[启动] 服务 http://0.0.0.0:{port}")
    print(f"[启动] 结算单目录: {SETTLEMENT_DIR}")
    print(f"[启动] 健康检查: http://127.0.0.1:{port}/api/health")
    print()
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    PORT = 5000
    account = _get_account_name()
    _try_start(PORT, account)
