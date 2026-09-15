"""
run_server.py — 启动入口
引用 dashboard_v2.api_server.create_app()
"""

import os
import sys

# 项目根目录
_project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _project_root)

from dashboard_v2.api_server import create_app

# 结算单目录（相对于项目根目录）
SETTLEMENT_DIR = os.path.join(_project_root, "结算单")

if __name__ == "__main__":
    # 清理旧进程
    import socket, subprocess, time
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sock.connect_ex(("127.0.0.1", 5000)) == 0:
        print("[启动] 5000 端口被占用，杀旧进程...")
        subprocess.run(
            'powershell -Command "Get-NetTCPConnection -LocalPort 5000 | Select-Object -Expand OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }"',
            capture_output=True, text=True
        )
        time.sleep(1)
        print("[启动] 旧进程已清理")
    sock.close()

    app = create_app(
        settlement_dir=SETTLEMENT_DIR,
        static_folder=os.path.join(_project_root, "static"),
        template_folder=os.path.join(_project_root, "templates"),
    )
    print(f"启动服务 http://0.0.0.0:5000")
    print(f"结算单目录: {SETTLEMENT_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
